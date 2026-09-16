"""AirPlay discovery, validation and resilient streaming (pyatv).

Verified against pyatv 0.18.0:
- ``pyatv.scan(loop, timeout=..., identifier=...)``
- ``pyatv.connect(config, loop)``
- ``BaseConfig.get_service(Protocol.RAOP).pairing`` -> ``PairingRequirement``
- ``AppleTV.stream.stream_file(io.BufferedIOBase)``
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Sequence

import pyatv
from pyatv.const import PairingRequirement, Protocol
from pyatv.interface import BaseConfig

logger = logging.getLogger(__name__)

__all__ = [
    "pick_devices",
    "DeviceInfo",
    "RetryPolicy",
    "summarize",
    "pick_device",
    "scan_devices",
    "stream_forever",
    "ACCESS_HINT",
]

ACCESS_HINT = (
    "RAOP pairing is not open on this device. In the Apple Home app, open the "
    "HomePod's settings and set 'Allow Speaker & TV Access' to "
    "'Anyone on the Same Network', then rescan."
)

_STREAMABLE = (PairingRequirement.NotNeeded, PairingRequirement.Optional)


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    address: str
    identifier: str
    raop_pairing: Optional[PairingRequirement]

    @property
    def streamable(self) -> bool:
        return self.raop_pairing in _STREAMABLE

    @property
    def pairing_label(self) -> str:
        return self.raop_pairing.name if self.raop_pairing else "NoRAOP"


def summarize(conf: BaseConfig) -> DeviceInfo:
    raop = conf.get_service(Protocol.RAOP)
    return DeviceInfo(
        name=conf.name,
        address=str(conf.address),
        identifier=str(conf.identifier),
        raop_pairing=raop.pairing if raop else None,
    )


def pick_device(devices: Sequence[DeviceInfo], name: str) -> DeviceInfo:
    """Select a device by exact (preferred) or unique partial name match."""
    wanted = name.strip().casefold()
    exact = [d for d in devices if d.name.casefold() == wanted]
    partial = [d for d in devices if wanted in d.name.casefold()]
    matches = exact or partial
    if not matches:
        known = ", ".join(sorted(d.name for d in devices)) or "none found"
        raise LookupError(f"No AirPlay device matching '{name}' (seen: {known})")
    if len(matches) > 1:
        names = ", ".join(sorted(d.name for d in matches))
        raise LookupError(f"Ambiguous device name '{name}': matches {names}")
    return matches[0]


def pick_devices(devices: Sequence[DeviceInfo], names: Sequence[str]) -> List[DeviceInfo]:
    """Resolve several device names, rejecting duplicates."""
    chosen: List[DeviceInfo] = []
    seen: set = set()
    for name in names:
        d = pick_device(devices, name)
        if d.identifier in seen:
            raise LookupError(f"Device '{d.name}' selected more than once")
        seen.add(d.identifier)
        chosen.append(d)
    return chosen


async def scan_devices(timeout: int = 6) -> List[DeviceInfo]:
    loop = asyncio.get_running_loop()
    confs = await pyatv.scan(loop, timeout=timeout)
    return [summarize(c) for c in confs]


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff, reset after a healthy streaming period."""

    initial_delay: float = 1.0
    max_delay: float = 30.0
    factor: float = 2.0
    reset_after: float = 10.0  # seconds of streaming that count as "healthy"

    def delays(self) -> Iterator[float]:
        d = self.initial_delay
        while True:
            yield d
            d = min(d * self.factor, self.max_delay)


@dataclass
class StreamStats:
    connects: int = 0
    failures: int = 0
    last_error: Optional[str] = None


async def stream_forever(
    identifier: str,
    open_reader: Callable[[], "object"],
    policy: RetryPolicy = RetryPolicy(),
    stop_event: Optional[asyncio.Event] = None,
    stats: Optional[StreamStats] = None,
    on_event: Optional[Callable[[str, Optional[object]], None]] = None,
    prefer_native: bool = True,
) -> None:
    """Stream to ``identifier`` until ``stop_event`` is set, reconnecting on
    any failure with capped exponential backoff.

    ``open_reader`` must return a *fresh* ``io.BufferedIOBase`` positioned at
    the live edge of the audio (see ``SinkSwitch`` + ``Mp3Pipe``).

    ``on_event`` (optional) receives ``("connecting", None)``,
    ``("connected", atv)`` and ``("disconnected", None)``; exactly one
    ``disconnected`` follows every ``connected``. Callback errors are logged
    and never break the watchdog.
    """

    def emit(event: str, payload: Optional[object] = None) -> None:
        if on_event is None:
            return
        try:
            on_event(event, payload)
        except Exception:  # noqa: BLE001 - never let UI break streaming
            logger.exception("on_event callback failed for %r", event)

    loop = asyncio.get_running_loop()
    stop_event = stop_event or asyncio.Event()
    stats = stats or StreamStats()
    delays = policy.delays()

    while not stop_event.is_set():
        atv = None
        delay = None
        announced = False
        streaming_started = None
        try:
            emit("connecting")
            confs = await pyatv.scan(loop, identifier=identifier, timeout=5)
            if stop_event.is_set():
                return
            if not confs:
                raise ConnectionError(f"device {identifier} not found on network")
            conf = confs[0]
            from .native_sender import NativeSession, command_for

            native_command = command_for(conf) if prefer_native else None
            if native_command:
                atv = await NativeSession.connect(native_command, open_reader())
            else:
                atv = await pyatv.connect(conf, loop)
            if stop_event.is_set():
                return  # stopped mid-connect; finally closes atv
            if native_command and on_event is None:
                # The CLI has no tray callback to apply a saved volume.
                # The helper starts muted, so give standalone streams the
                # same 50% default as a new bridge configuration.
                await atv.audio.set_volume(50.0)
            stats.connects += 1
            announced = True
            emit("connected", atv)
            logger.info("Connected to %s (%s) - streaming", conf.name, conf.address)
            streaming_started = time.monotonic()
            if native_command:
                await atv.run()
            else:
                await atv.stream.stream_file(open_reader())
            logger.info("Stream ended")
            if stop_event.is_set():
                return
            raise ConnectionError("stream ended unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - watchdog boundary
            if stop_event.is_set():
                return
            stats.failures += 1
            stats.last_error = f"{type(exc).__name__}: {exc}"
            # Only actual streaming time counts as healthy: a slow-failing
            # scan/connect (stale mDNS record, TCP timeout) must not reset
            # the backoff, or the delay stays pinned at initial_delay.
            if (
                streaming_started is not None
                and time.monotonic() - streaming_started >= policy.reset_after
            ):
                delays = policy.delays()  # it streamed fine for a while
            delay = next(delays)
            logger.warning("%s - reconnecting in %.1fs", stats.last_error, delay)
        finally:
            if announced:
                emit("disconnected")
            if atv is not None:
                try:
                    pending = atv.close()  # pyatv 0.18: returns cleanup tasks
                    if pending:
                        await asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True),
                            timeout=3,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    logger.debug("device teardown incomplete", exc_info=True)
        # Failed connections and their UI state must be retired before
        # sleeping, including when the backoff has reached its 30s cap.
        if delay is not None and not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
