"""PCM -> MP3 pipeline feeding a :class:`StreamBuffer`.

MP3 is used as the wire format because it is self-framing (pyatv/miniaudio can
sync onto it mid-stream) and is an officially supported ``stream_file`` input.
"""
from __future__ import annotations

import io
import threading
from typing import Callable, Optional, Sequence

from .stream_buffer import StreamBuffer

__all__ = ["Mp3Encoder", "Mp3Pipe", "SinkSwitch", "FanOutSink"]

PcmSink = Callable[[bytes], None]


class Mp3Encoder:
    """Thin wrapper around ``lameenc`` for interleaved int16 PCM."""

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        bitrate_kbps: int = 320,
        quality: int = 2,
    ) -> None:
        import lameenc  # deferred so tests can use a fake encoder

        enc = lameenc.Encoder()
        enc.set_bit_rate(bitrate_kbps)
        enc.set_in_sample_rate(sample_rate)
        enc.set_channels(channels)
        enc.set_quality(quality)  # 2 = high quality
        enc.silence()  # suppress lame's stderr chatter
        self._enc = enc

    def encode(self, pcm: bytes) -> bytes:
        return bytes(self._enc.encode(pcm))

    def flush(self) -> bytes:
        return bytes(self._enc.flush())


class Mp3Pipe:
    """Feeds PCM chunks through an encoder into a readable MP3 stream."""

    def __init__(self, encoder, buffer: Optional[StreamBuffer] = None) -> None:
        self._encoder = encoder
        self.buffer = buffer if buffer is not None else StreamBuffer()
        self._finished = False
        # feed_pcm runs on the capture thread while finish() runs on the
        # engine loop (every reconnect). lameenc releases the GIL during
        # encode, so an unserialized flush() could run C-level LAME code
        # concurrently with an in-flight encode() on the same instance.
        self._lock = threading.Lock()

    def feed_pcm(self, pcm_chunk: bytes) -> None:
        if not pcm_chunk:
            return
        with self._lock:
            if self._finished:
                return
            mp3 = self._encoder.encode(pcm_chunk)
            if not mp3:
                return
            try:
                self.buffer.write(mp3)
            except ValueError:
                # The buffer was finished underneath us (pyatv's dropped
                # reader closes it via GC at an arbitrary time). Raising here
                # would kill the shared capture thread - drop the chunk.
                return

    def finish(self) -> None:
        """Flush the encoder tail and mark EOF on the buffer."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            tail = self._encoder.flush()
        if tail:
            try:
                self.buffer.write(tail)
            except ValueError:
                pass  # buffer already finished elsewhere
        self.buffer.finish()

    def reader(self) -> io.BufferedReader:
        """A ``BufferedIOBase`` view, as required by pyatv's ``stream_file``."""
        return io.BufferedReader(self.buffer)


class SinkSwitch:
    """A swappable PCM sink.

    The capture thread holds one stable callable while the AirPlay side swaps
    in a fresh :class:`Mp3Pipe` on every (re)connect, guaranteeing each RAOP
    session starts on a clean MP3 stream.
    """

    def __init__(self) -> None:
        self._sink: Optional[PcmSink] = None
        self._lock = threading.Lock()

    def __call__(self, chunk: bytes) -> None:
        with self._lock:
            sink = self._sink
        if sink is not None:
            sink(chunk)

    def set(self, sink: Optional[PcmSink]) -> None:
        with self._lock:
            self._sink = sink


class FanOutSink:
    """Forwards each PCM chunk to every child sink.

    Used for multi-device streaming: the capture thread feeds one FanOutSink,
    which fans out to one :class:`SinkSwitch` per AirPlay session.
    """

    def __init__(self, sinks: Sequence[PcmSink]) -> None:
        self._sinks = tuple(sinks)

    def __call__(self, chunk: bytes) -> None:
        for sink in self._sinks:
            sink(chunk)
