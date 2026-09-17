"""Background streaming engine for the tray app.

Owns an asyncio loop on a daemon thread. Public methods are thread-safe and
block until the loop has applied the command, so callers (tray callbacks)
always get back a consistent :class:`Snapshot`.

Everything with side effects — device scanning, RAOP streaming, audio
capture, MP3 encoding — is injected, keeping the engine unit-testable
without pyatv, sound hardware, or a network.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import airplay
from .airplay import DeviceInfo, RetryPolicy, StreamStats
from .mp3_pipe import FanOutSink, Mp3Pipe, SinkSwitch
from .pcm_pipe import PcmPipe
from .stream_buffer import StreamBuffer

logger = logging.getLogger(__name__)

__all__ = ["BridgeEngine", "EngineState", "Snapshot"]

CALL_TIMEOUT = 20.0
RESTART_WINDOW = 60.0  # seconds
MAX_RESTARTS_IN_WINDOW = 3
CAPTURE_REOPEN_ATTEMPTS = 5  # a just-died endpoint needs seconds to come back
CAPTURE_REOPEN_DELAY = 2.0
# Live-stream buffer bound, in SECONDS of audio. The RAOP consumer only ever
# drains at realtime speed, so every byte of backlog is permanent added
# latency: audio beyond this bound is dropped (brief glitch) rather than
# replayed behind live forever.
#
# Kept tight deliberately. Capture starts feeding as soon as a device
# connects, but pyatv only begins consuming after its RTSP negotiation, and
# whatever piles up in that window becomes permanent session delay - live
# measurement showed 886ms of standing backlog at a 4s bound. The capture
# thread also injects silence when the PC is quiet, so a tight bound cannot
# underrun. Jitter is absorbed by the receiver's own buffer (raop_latency).
LIVE_BUFFER_SECONDS = 0.4
# Multi-room drift correction. Each device runs its own RAOP session with
# its own buffer, and the backlog standing in that buffer IS how far behind
# live it plays. Devices connect seconds apart and independently discard
# different amounts of startup backlog, so without correction they settle at
# different offsets - measured 21ms typical and up to 320ms between two
# HomePods, which is audible as a smear. Trim laggards back to the most-live
# device whenever they drift past the threshold.
RESYNC_INTERVAL = 1.0
RESYNC_THRESHOLD_SECONDS = 0.02
# Where every device should sit relative to live. Equalising the rooms
# against each other is not enough: whatever backlog they happen to share
# at connect is permanent added delay, because RAOP paces at exact realtime
# and never catches up. Measured 107-128ms of standing backlog surviving a
# whole session. Trim toward a small working margin instead - enough to
# ride out scheduling hiccups, with network jitter absorbed by the
# receiver's own buffer (raop_latency).
RESYNC_TARGET_SECONDS = 0.05


class EngineState(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    STREAMING = "streaming"


@dataclass(frozen=True)
class Snapshot:
    state: EngineState
    devices: Tuple[DeviceInfo, ...]  # streamable devices from last scan
    selected: Tuple[str, ...]  # stable device identifiers chosen by the user
    connected: Tuple[str, ...]  # stable identifiers with a live RAOP session
    volume: float  # master volume (also the default for devices w/o override)
    capture_restarts: int = 0  # sessions auto-restarted after capture death
    volumes: Tuple[Tuple[str, float], ...] = ()  # (identifier, effective) per selected

    def device_labels(self) -> Dict[str, str]:
        """Friendly, unique labels; identities remain independent of names."""
        counts = collections.Counter(d.name for d in self.devices)
        labels = {
            d.identifier: d.name if counts[d.name] == 1 else f"{d.name} ({d.address})"
            for d in self.devices
        }
        for identifier in self.selected + self.connected:
            labels.setdefault(identifier, identifier)
        # Include offline selections and handle names that already look
        # like generated labels, so popup callbacks always map one-to-one.
        while len(set(labels.values())) != len(labels):
            collisions = collections.Counter(labels.values())
            labels = {
                identifier: label if collisions[label] == 1 else f"{label} [{identifier}]"
                for identifier, label in labels.items()
            }
        return labels

    def device_label(self, identifier: str) -> str:
        return self.device_labels().get(identifier, identifier)


class BridgeEngine:
    def __init__(
        self,
        *,
        scan_fn: Optional[Callable] = None,
        stream_fn: Optional[Callable] = None,
        capture_factory: Optional[Callable] = None,
        encoder_factory: Optional[Callable] = None,
        on_change: Optional[Callable[[Snapshot], None]] = None,
        on_alert: Optional[Callable[[str], None]] = None,
        volume: float = 50.0,
        device_volumes: Optional[Dict[str, float]] = None,
        bitrate: int = 320,
        quality: int = 2,
    ) -> None:
        self._scan_fn = scan_fn or airplay.scan_devices
        self._stream_fn = stream_fn or airplay.stream_forever
        self._default_stream = stream_fn is None
        self._capture_factory = capture_factory or self._default_capture_factory
        # None => raw PCM (production). Tests inject an encoder to exercise
        # the MP3 path without lameenc.
        self._encoder_factory = encoder_factory
        self._on_change = on_change
        self._on_alert = on_alert
        self._bitrate, self._quality = bitrate, quality
        self._volume = max(0.0, min(100.0, float(volume)))
        self._device_volumes: Dict[str, float] = {
            str(k): max(0.0, min(100.0, float(v)))
            for k, v in (device_volumes or {}).items()
        }
        # Per-device apply pipeline: latest desired level + a worker task
        # draining it, so overlapping sends always land in submission order.
        self._volume_apply: Dict[str, float] = {}
        self._volume_tasks: Dict[str, asyncio.Task] = {}

        self._devices: List[DeviceInfo] = []
        self._scan_generation = 0
        self._selected: List[str] = []
        self._connected: set = set()
        self._atvs: Dict[str, object] = {}
        self._pipes: Dict[str, Mp3Pipe] = {}
        self._switches: Dict[str, SinkSwitch] = {}
        self._capture = None
        self._stop_evt: Optional[asyncio.Event] = None
        self._session = None
        self._resync_task: Optional[asyncio.Task] = None
        self._state = EngineState.IDLE
        self._restart_times: collections.deque = collections.deque(maxlen=16)
        self._capture_restarts = 0
        self._capture_generation = 0
        # Serializes session mutations (user start/stop/reselect vs capture
        # recovery): without it, a recovery restart interleaving with a user
        # Disconnect could rebuild the session AFTER the user stopped it.
        self._session_lock = asyncio.Lock()
        # Set before a user stop takes the lock, so a recovery retry loop
        # holding it can bail out promptly instead of making the user wait.
        self._stop_requested = False

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="bridge-engine", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------- plumbing
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(CALL_TIMEOUT)

    @staticmethod
    def _default_capture_factory(sink, on_failure=None):
        from .capture import LoopbackCapture  # Windows-only, deferred

        return LoopbackCapture(sink, on_failure=on_failure)

    # ----------------------------------------------------- public (thread-safe)
    def snapshot(self) -> Snapshot:
        return self._call(self._async_snapshot())

    def rescan(self, timeout: int = 6) -> Snapshot:
        return self._call(self._rescan(timeout))

    def set_selected(self, identifiers: Sequence[str]) -> Snapshot:
        return self._call(self._set_selected(list(dict.fromkeys(identifiers))))

    def toggle_device(self, name: str) -> Snapshot:
        return self._call(self._toggle(name))

    def start_selected(self) -> Snapshot:
        return self._call(self._user_start())

    def stop_streaming(self) -> Snapshot:
        return self._call(self._stop_and_snap())

    def set_volume(self, level: float) -> Snapshot:
        """Master volume: sets EVERY device to this level (clears overrides)."""
        return self._call(self._set_volume(level))

    def set_volume_nowait(self, level: float) -> None:
        """Fire-and-forget master volume for UI threads: never blocks.

        Raises RuntimeError only when the loop is closed (engine shut down)."""
        future = asyncio.run_coroutine_threadsafe(self._set_volume(level), self._loop)
        future.add_done_callback(lambda f: f.exception())  # retrieve, don't warn

    def set_device_volume(self, name: str, level: float) -> Snapshot:
        """Per-device volume: overrides the master for this device only."""
        return self._call(self._set_device_volume(name, level))

    def set_device_volume_nowait(self, name: str, level: float) -> None:
        """Fire-and-forget per-device volume for UI threads: never blocks."""
        future = asyncio.run_coroutine_threadsafe(
            self._set_device_volume(name, level), self._loop
        )
        future.add_done_callback(lambda f: f.exception())  # retrieve, don't warn

    def nudge_volume(self, delta: float) -> Snapshot:
        return self._call(self._set_volume(self._volume + delta))

    def shutdown(self) -> None:
        if not self._thread.is_alive():
            return
        try:
            self._call(self._shutdown_tasks())
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("error stopping session during shutdown")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._loop.is_running():
            self._loop.close()

    # -------------------------------------------------------------- internals
    def _effective_volume(self, name: str) -> float:
        return self._device_volumes.get(name, self._volume)

    def _make_snapshot(self) -> Snapshot:
        return Snapshot(
            state=self._state,
            devices=tuple(d for d in self._devices if d.streamable),
            selected=tuple(self._selected),
            connected=tuple(sorted(self._connected)),
            volume=self._volume,
            capture_restarts=self._capture_restarts,
            volumes=tuple((n, self._effective_volume(n)) for n in self._selected),
        )

    def _alert(self, message: str) -> None:
        if self._on_alert is None:
            return
        try:
            self._on_alert(message)
        except Exception:  # noqa: BLE001 - alerts are best-effort
            logger.exception("on_alert callback failed")

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(self._make_snapshot())
        except Exception:  # noqa: BLE001 - UI errors must not kill the engine
            logger.exception("on_change callback failed")

    async def _async_snapshot(self) -> Snapshot:
        return self._make_snapshot()

    async def _rescan(self, timeout: int) -> Snapshot:
        self._scan_generation += 1
        generation = self._scan_generation
        devices = await self._scan_fn(timeout=timeout)
        if generation == self._scan_generation:
            self._devices = devices
            self._notify()
        return self._make_snapshot()

    async def _shutdown_tasks(self) -> None:
        await self._locked_stop()
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _user_start(self) -> Snapshot:
        async with self._session_lock:
            # User-initiated (re)connect: fresh capture-restart budget, so a
            # session retried after "Connect again" is not killed by stale
            # failure timestamps from the previous session.
            self._restart_times.clear()
            return await self._start(list(self._selected))

    async def _set_selected(self, names: List[str]) -> Snapshot:
        async with self._session_lock:
            return await self._set_selected_locked(names)

    async def _set_selected_locked(self, identifiers: List[str]) -> Snapshot:
        if self._session is not None:
            self._restart_times.clear()  # user-initiated restart
            return await self._start(identifiers)
        self._selected = identifiers
        self._notify()
        return self._make_snapshot()

    async def _toggle(self, name: str) -> Snapshot:
        async with self._session_lock:
            sel = list(self._selected)
            if name in sel:
                sel.remove(name)
            else:
                sel.append(name)
            return await self._set_selected_locked(sel)

    async def _start(self, names: List[str]) -> Snapshot:
        await self._stop_session()
        self._selected = names
        available = {d.identifier: d for d in self._devices if d.streamable}
        targets = [available[n] for n in names if n in available]
        if not targets:
            self._notify()
            return self._make_snapshot()

        self._switches = {t.identifier: SinkSwitch() for t in targets}
        generation = self._capture_generation
        try:
            self._capture = self._capture_factory(
                FanOutSink(list(self._switches.values())),
                on_failure=lambda: self._capture_failure(generation),
            )
            self._capture.start()
        except Exception:
            # A half-started capture must not linger: _stop_session skips it
            # while _session is None, so it would leak a native PortAudio
            # instance on every failed Connect.
            capture, self._capture = self._capture, None
            self._switches.clear()
            if capture is not None:
                await self._stop_capture(capture)
            self._state = EngineState.IDLE
            self._notify()
            raise
        fmt = self._capture.fmt
        if fmt.channels > 2:
            # lameenc rejects >2 channels eagerly; without this gate the
            # session would flap CONNECTING/STREAMING forever with no alert.
            logger.error("unsupported %d-channel output device", fmt.channels)
            capture, self._capture = self._capture, None
            self._switches.clear()
            await self._stop_capture(capture)
            self._state = EngineState.IDLE
            self._notify()
            self._alert(
                f"Surround ({fmt.channels}-channel) output devices aren't "
                "supported. Switch Windows output to a stereo device, then "
                "Connect again."
            )
            return self._make_snapshot()

        self._stop_evt = asyncio.Event()
        self._state = EngineState.CONNECTING
        tasks = [
            self._stream_fn(
                t.identifier,
                self._make_open_reader(t, fmt),
                policy=RetryPolicy(),
                stop_event=self._stop_evt,
                stats=StreamStats(),
                on_event=self._make_on_event(t.identifier),
                # The native sender supports one selected endpoint. Multiple
                # selections use RAOP; single-selection pair relay is unsupported.
                **({"prefer_native": len(targets) == 1} if self._default_stream else {}),
            )
            for t in targets
        ]
        self._session = asyncio.gather(*tasks, return_exceptions=True)
        # Runs for a single device too: standing backlog is added latency
        # regardless of how many rooms are playing.
        self._resync_task = asyncio.get_running_loop().create_task(
            self._keep_devices_in_sync(fmt)
        )
        self._notify()
        return self._make_snapshot()

    async def _keep_devices_in_sync(self, fmt) -> None:
        """Hold every device close to live, and therefore to each other."""
        byte_rate = fmt.sample_rate * fmt.channels * fmt.sample_width
        threshold = int(RESYNC_THRESHOLD_SECONDS * byte_rate)
        target = int(RESYNC_TARGET_SECONDS * byte_rate)
        try:
            while self._session is not None and not (
                self._stop_evt is not None and self._stop_evt.is_set()
            ):
                await asyncio.sleep(RESYNC_INTERVAL)
                for pipe in list(self._pipes.values()):
                    if pipe.buffer.closed:
                        continue
                    excess = pipe.buffer._size - target
                    if excess > threshold:
                        freed = pipe.buffer.trim_oldest(excess)
                        logger.debug(
                            "resync: trimmed %.0f ms of standing backlog",
                            freed / byte_rate * 1000,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - sync polish must never break audio
            logger.debug("device resync stopped", exc_info=True)

    def _capture_failure(self, generation: int) -> None:
        """Called from the capture thread when its read loop dies."""
        try:
            self._loop.call_soon_threadsafe(
                lambda: self._loop.create_task(self._recover_capture(generation))
            )
        except RuntimeError:
            pass  # loop already stopped during shutdown

    async def _recover_capture(self, generation: int) -> None:
        """Restart the session with a fresh capture (re-detects the output
        device - covers sleep/resume and device changes). Rapid repeated
        failures stop the session instead of restart-looping forever."""
        async with self._session_lock:
            if generation != self._capture_generation:
                return  # a delayed callback from a replaced capture
            await self._recover_capture_locked()

    async def _recover_capture_locked(self) -> None:
        # Guard re-checked under the lock: a user stop that won the lock
        # first leaves _session None, and this becomes a no-op instead of
        # resurrecting a session the user just disconnected.
        if self._session is None or (
            self._stop_evt is not None and self._stop_evt.is_set()
        ):
            return
        now = time.monotonic()
        self._restart_times.append(now)
        recent = [t for t in self._restart_times if now - t <= RESTART_WINDOW]
        if len(recent) > MAX_RESTARTS_IN_WINDOW:
            logger.error("audio capture keeps failing; stopping session")
            await self._stop_session()
            self._notify()
            self._alert(
                "Audio capture keeps failing - streaming stopped. "
                "Check the output device, then Connect again."
            )
            return
        logger.warning(
            "audio capture failed; restarting session (re-detecting output device)"
        )
        self._capture_restarts += 1
        # The endpoint is often still resetting right after it died (display
        # sleep on an HDMI/DisplayPort audio device makes pa.open fail with
        # 'Insufficient memory' / 'Device unavailable' for a few seconds).
        # Retry a few times instead of letting the exception kill this task,
        # which left the app idle and silent with no alert.
        for attempt in range(1, CAPTURE_REOPEN_ATTEMPTS + 1):
            if self._stop_requested:
                return  # user hit Disconnect while we were retrying
            try:
                await self._start(list(self._selected))
                return
            except Exception:  # noqa: BLE001 - device may still be settling
                logger.warning(
                    "capture reopen attempt %d/%d failed",
                    attempt,
                    CAPTURE_REOPEN_ATTEMPTS,
                    exc_info=True,
                )
                await asyncio.sleep(CAPTURE_REOPEN_DELAY)
        logger.error("audio capture could not be reopened; stopping session")
        await self._stop_session()
        self._notify()
        self._alert(
            "Lost the audio output device and could not reopen it - "
            "streaming stopped. Check the output device, then Connect again."
        )

    def _live_buffer_bytes(self, fmt=None) -> int:
        if self._encoder_factory is None and fmt is not None:
            byte_rate = fmt.sample_rate * fmt.channels * fmt.sample_width
        else:
            byte_rate = self._bitrate * 1000 / 8
        return max(64 * 1024, int(byte_rate * LIVE_BUFFER_SECONDS))

    def _make_pipe(self, fmt):
        """Raw PCM by default - pyatv sends PCM to the device regardless, and
        an MP3 container costs ~1.6s of decoder-init stall. An explicit
        encoder_factory (tests) still selects the MP3 path."""
        cap = self._live_buffer_bytes(fmt)
        if self._encoder_factory is not None:
            return Mp3Pipe(self._encoder_factory(fmt), buffer=StreamBuffer(max_bytes=cap))
        return PcmPipe.live(
            fmt.sample_rate,
            fmt.channels,
            fmt.sample_width,
            buffer_seconds=LIVE_BUFFER_SECONDS,
            # Every device starts its stream the same distance from live,
            # whenever it happens to connect - the receiver's playback
            # schedule is fixed from where the stream began.
            prime_seconds=RESYNC_TARGET_SECONDS,
        )

    def _make_open_reader(self, target: DeviceInfo, fmt):
        def open_reader():
            old = self._pipes.get(target.identifier)
            if old is not None:
                old.finish()
            pipe = self._make_pipe(fmt)
            if self._stop_evt is not None and self._stop_evt.is_set():
                # Session is stopping but this task connected anyway (it was
                # past its stop check). Hand back an already-EOF stream so it
                # winds down immediately instead of streaming live audio
                # through the 10s stop timeout.
                pipe.finish()
            else:
                self._switches[target.identifier].set(pipe.feed_pcm)
                self._pipes[target.identifier] = pipe
            return pipe.reader()

        return open_reader

    def _make_on_event(self, name: str):
        def on_event(event: str, payload=None) -> None:
            # Runs on the engine loop thread (inside the stream task).
            if event == "connected":
                if self._stop_evt is not None and self._stop_evt.is_set():
                    return  # late connect while stopping; don't flip state
                self._atvs[name] = payload
                self._connected.add(name)
                self._state = EngineState.STREAMING
                self._queue_volume(name)  # push this device's effective level
            elif event == "disconnected":
                self._atvs.pop(name, None)
                self._connected.discard(name)
                if self._stop_evt is not None and not self._stop_evt.is_set():
                    self._state = (
                        EngineState.STREAMING
                        if self._connected
                        else EngineState.CONNECTING
                    )
            else:  # "connecting"
                return
            self._notify()

        return on_event

    def _queue_volume(self, name: str) -> None:
        """Queue the device's effective level; a per-device worker drains the
        LATEST queued value, so overlapping applies land in submission order
        even when a laggy HomePod makes one RTSP round-trip outlast the next
        send."""
        self._volume_apply[name] = self._effective_volume(name)
        task = self._volume_tasks.get(name)
        if task is not None and not task.done():
            return  # worker already draining; it will pick up the new value
        self._volume_tasks[name] = asyncio.get_running_loop().create_task(
            self._drain_volume(name)
        )

    async def _drain_volume(self, name: str) -> None:
        while True:
            atv = self._atvs.get(name)
            level = self._volume_apply.get(name)
            if atv is None or level is None:
                self._volume_apply.pop(name, None)
                return
            try:
                await atv.audio.set_volume(level)
            except Exception:  # noqa: BLE001 - volume is best-effort
                logger.debug("volume apply failed", exc_info=True)
            # Done only if nothing newer was queued AND the connection we
            # just applied to is still the current one: a reconnect during a
            # hung apply re-queues the SAME level, which a pure value
            # comparison would wrongly treat as already delivered - leaving
            # the fresh session at pyatv's default volume.
            if (
                self._volume_apply.get(name) == level
                and self._atvs.get(name) is atv
            ):
                self._volume_apply.pop(name, None)
                return

    async def _drained(self, names) -> None:
        """Wait for queued applies to land - keeps the blocking set_volume /
        set_device_volume semantics of returning after the device heard it."""
        for name in names:
            task = self._volume_tasks.get(name)
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    if task.cancelled():
                        continue  # a concurrent stop killed the worker; the
                        # session is gone - nothing left to wait for
                    raise  # WE were cancelled - propagate
                except Exception:  # noqa: BLE001
                    pass

    async def _set_volume(self, level: float) -> Snapshot:
        self._volume = max(0.0, min(100.0, float(level)))
        # The master is "set everything": per-device overrides follow it.
        self._device_volumes.clear()
        names = list(self._atvs)
        for name in names:
            self._queue_volume(name)
        await self._drained(names)
        self._notify()
        return self._make_snapshot()

    async def _set_device_volume(self, name: str, level: float) -> Snapshot:
        self._device_volumes[str(name)] = max(0.0, min(100.0, float(level)))
        if name in self._atvs:
            self._queue_volume(name)
            await self._drained([name])
        self._notify()
        return self._make_snapshot()

    async def _locked_stop(self) -> None:
        self._stop_requested = True  # abort any in-flight recovery retries
        try:
            async with self._session_lock:
                await self._stop_session()
        finally:
            self._stop_requested = False

    async def _stop_and_snap(self) -> Snapshot:
        await self._locked_stop()
        self._notify()
        return self._make_snapshot()

    async def _stop_capture(self, capture) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(None, capture.stop)
        except Exception:  # noqa: BLE001 - a dead device may make stop raise
            logger.exception("capture stop failed")

    async def _stop_session(self) -> None:
        self._capture_generation += 1
        capture, self._capture = self._capture, None
        if self._session is None:
            self._state = EngineState.IDLE
            if capture is not None:  # e.g. left behind by a failed start
                await self._stop_capture(capture)
            return
        # Everything after the session wait runs in `finally`: if any step
        # raises, skipping the state reset would strand the engine in
        # STREAMING with a dead session and recovery abandoned.
        try:
            self._stop_evt.set()
            for pipe in self._pipes.values():
                pipe.finish()  # unblocks stream readers -> sessions wind down
            try:
                await asyncio.wait_for(self._session, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001
                logger.exception("session ended with error")
        finally:
            if capture is not None:
                await self._stop_capture(capture)
            if self._resync_task is not None:
                self._resync_task.cancel()
                self._resync_task = None
            for task in self._volume_tasks.values():
                task.cancel()
            self._volume_tasks.clear()
            self._volume_apply.clear()
            self._pipes.clear()
            self._switches.clear()
            self._atvs.clear()
            self._connected.clear()
            self._session = None
            self._stop_evt = None
            self._state = EngineState.IDLE
