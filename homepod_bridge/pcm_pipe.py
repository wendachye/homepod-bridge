"""Live PCM -> WAV stream feeding pyatv, replacing the MP3 round trip.

pyatv transmits **raw PCM** to the receiver regardless of what we hand it
(``airplayv2.py`` sets ``"ct": 1, "audioFormat": 0x800``), so encoding to MP3
only meant pyatv immediately decoded it back again - a lossy transcode that
saved no bandwidth and cost ~1.65 s of latency:

pyatv's decoder init will not commit until it has filled its entire 64 KiB
``SemiSeekableBuffer``. At 320 kbps that is 65536 / 40000 = 1.64 s of wall
time, and because RAOP paces strictly at realtime and never catches up, that
startup stall becomes permanent delay for the whole session. Measured on
this machine: MP3 ``open_source()`` 1665 ms vs WAV 22 ms.

The container is a WAV header with a large declared length followed by live
frames. The declared size matters: ``0xFFFFFFFF`` is treated as a scan-me
sentinel and makes the parser read forever (measured: still stalled after
12 s), so :data:`DECLARED_DATA_BYTES` stays safely below it while still
covering hours of audio.
"""
from __future__ import annotations

import io
import struct
import threading
from typing import Optional

from .stream_buffer import StreamBuffer

__all__ = ["PcmPipe", "wav_header", "DECLARED_DATA_BYTES"]

#: Declared WAV data length. Must not be 0xFFFFFFFF (a sentinel that sends
#: the parser scanning the whole stream). ~4.29 GB covers ~6.2 hours of
#: 48 kHz stereo 16-bit; a longer session simply reconnects with a fresh
#: header, which the watchdog already handles.
DECLARED_DATA_BYTES = 0xFFFFFF00


def wav_header(
    sample_rate: int,
    channels: int,
    sample_width: int = 2,
    data_bytes: int = DECLARED_DATA_BYTES,
) -> bytes:
    """A 44-byte PCM WAV header for a stream of open-ended length."""
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    riff_size = min(data_bytes + 36, 0xFFFFFF24)  # keep clear of the sentinel
    return (
        b"RIFF"
        + struct.pack("<I", riff_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack(
            "<IHHIIHH",
            16,  # PCM fmt chunk size
            1,  # format = PCM
            channels,
            sample_rate,
            byte_rate,
            block_align,
            sample_width * 8,
        )
        + b"data"
        + struct.pack("<I", data_bytes)
    )


class _HeaderThenAudio(io.RawIOBase):
    """Serves the WAV header, then the live audio buffer.

    The header must NOT live inside the bounded buffer: that buffer drops
    its oldest bytes to stay near-live, and the header is by definition the
    oldest bytes. Losing it leaves the consumer with unidentifiable raw PCM.
    """

    def __init__(
        self, header: bytes, buffer: StreamBuffer, prime_bytes: Optional[int] = None
    ) -> None:
        self._header = header
        self._pos = 0
        self._buffer = buffer
        self._prime_bytes = prime_bytes
        self._primed = False

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, b) -> int:  # type: ignore[override]
        remaining = len(self._header) - self._pos
        if remaining > 0:
            n = min(len(b), remaining)
            b[:n] = self._header[self._pos : self._pos + n]
            self._pos += n
            return n
        if not self._primed:
            self._primed = True
            if self._prime_bytes is not None:
                # A receiver plays each sample at an absolute time derived
                # from where its stream STARTED, so whatever backlog sat here
                # when it began reading becomes that device's permanent
                # offset. Devices connect seconds apart, which is why they
                # drifted apart. Starting every device at the same distance
                # from live puts them on the same schedule - and discards
                # backlog, so it lowers latency rather than adding any.
                excess = self._buffer._size - self._prime_bytes
                if excess > 0:
                    self._buffer.trim_oldest(excess)
        return self._buffer.readinto(b)

    def close(self) -> None:
        # Preserve the buffer's own close semantics (a dropped reader ends
        # the stream) that the engine's reconnect path relies on.
        try:
            self._buffer.close()
        finally:
            super().close()


class PcmPipe:
    """Feeds captured PCM straight through as a live WAV stream.

    Interface-compatible with :class:`~homepod_bridge.mp3_pipe.Mp3Pipe` so
    the engine can build either.
    """

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        sample_width: int = 2,
        buffer: Optional[StreamBuffer] = None,
        prime_seconds: Optional[float] = None,
    ) -> None:
        frame = max(1, channels * sample_width)
        self.buffer = (
            buffer if buffer is not None else StreamBuffer(align=frame)
        )
        self._prime_bytes = (
            None
            if prime_seconds is None
            else int(prime_seconds * sample_rate * channels * sample_width)
        )
        self._header = wav_header(sample_rate, channels, sample_width)
        self._finished = False
        self._lock = threading.Lock()

    def feed_pcm(self, pcm_chunk: bytes) -> None:
        if not pcm_chunk:
            return
        with self._lock:
            if self._finished:
                return
            try:
                self.buffer.write(pcm_chunk)
            except ValueError:
                # Buffer finished underneath us (a dropped reader gets
                # GC-closed at an arbitrary time). Raising here would kill
                # the shared capture thread - drop the chunk instead.
                return

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self.buffer.finish()

    def reader(self) -> io.BufferedReader:
        """A ``BufferedIOBase`` view, as required by pyatv's ``stream_file``."""
        return io.BufferedReader(
            _HeaderThenAudio(self._header, self.buffer, self._prime_bytes)
        )
