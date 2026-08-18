"""Control the receiver-side buffer that pyatv asks AirPlay devices to hold.

pyatv hardcodes the playout delay in ``protocols/raop/protocols/__init__.py``::

    self.latency = 22050 + self.sample_rate   # 66150 frames = 1.500s @ 44.1kHz

and exposes no setting for it, so this patches the one method that assigns
it. The value never travels in an RTSP header - it reaches the receiver as
the offset between the RTP timestamp stamped on audio packets and the
playhead announced in sync packets, both derived from this attribute, so
changing it alone is self-consistent.

Lower means less delay but less tolerance for network jitter: audio that
arrives later than the reserved buffer is dropped. pyatv itself advertises
``latencyMin 11025`` (250 ms) / ``latencyMax 88200`` (2 s), and OwnTone - a
mature AirPlay 2 sender that drives HomePods - uses 250 ms. We default to a
conservative 500 ms; raise it if playback breaks up on a busy network.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_LATENCY_SECONDS",
    "MAX_LATENCY_SECONDS",
    "MIN_LATENCY_SECONDS",
    "apply",
    "restore",
]

DEFAULT_LATENCY_SECONDS = 0.5
#: pyatv advertises this range in the AirPlay 2 SETUP body; asking for
#: something outside what we ourselves advertised is not safe.
MIN_LATENCY_SECONDS = 0.25
MAX_LATENCY_SECONDS = 2.0

_applied = False
_original_reset = None  # pyatv's own method, kept so restore() is exact
#: Read at the start of every session, so it can be changed at runtime and
#: picked up by the next (re)connect without restarting the app.
_seconds = DEFAULT_LATENCY_SECONDS


def current() -> float:
    return _seconds


def set_latency(seconds: float) -> float:
    """Change the hold for the NEXT session; returns the clamped value."""
    global _seconds
    _seconds = max(MIN_LATENCY_SECONDS, min(MAX_LATENCY_SECONDS, float(seconds)))
    return _seconds


def apply(seconds: Optional[float] = None) -> bool:
    """Patch pyatv so RAOP sessions use ``seconds`` of receiver buffer.

    Idempotent, best-effort, and safe to call before any connection. Returns
    True when the patch is in place. ``HOMEPOD_BRIDGE_RAOP_LATENCY``
    overrides the argument, so stock behaviour can be restored with
    ``set HOMEPOD_BRIDGE_RAOP_LATENCY=1.5`` and no code change.
    """
    global _applied, _original_reset
    if _applied:
        return True

    override = os.environ.get("HOMEPOD_BRIDGE_RAOP_LATENCY")
    if override:
        try:
            seconds = float(override)
        except ValueError:
            logger.warning("ignoring invalid HOMEPOD_BRIDGE_RAOP_LATENCY=%r", override)
    if seconds is None:
        seconds = DEFAULT_LATENCY_SECONDS
    set_latency(seconds)

    try:
        from pyatv.protocols.raop.protocols import StreamContext
    except Exception:  # noqa: BLE001 - pyatv layout changed; keep streaming
        logger.debug("RAOP latency patch unavailable", exc_info=True)
        return False

    _original_reset = original_reset = StreamContext.reset

    def reset(self) -> None:  # runs at the start of every send_audio
        original_reset(self)
        frames = int(_seconds * self.sample_rate)
        logger.info(
            "RAOP latency %d -> %d frames (%.0f ms @ %d Hz)",
            self.latency, frames, frames / self.sample_rate * 1000, self.sample_rate,
        )
        self.latency = frames

    StreamContext.reset = reset  # type: ignore[method-assign]
    _applied = True
    return True


def restore() -> None:
    """Put pyatv's own latency behaviour back (used by tests)."""
    global _applied, _original_reset
    if _original_reset is not None:
        from pyatv.protocols.raop.protocols import StreamContext

        StreamContext.reset = _original_reset  # type: ignore[method-assign]
        _original_reset = None
    _applied = False
