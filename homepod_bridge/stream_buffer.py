"""Thread-safe, bounded, blocking byte stream.

Bridges a producer thread (audio capture -> MP3 encoder) to a consumer
(pyatv's ``stream_file``) that reads it like a file. Non-seekable, blocking
reads, explicit EOF via :meth:`finish`.
"""
from __future__ import annotations

import collections
import io
import threading
from typing import Deque

__all__ = ["StreamBuffer"]


class StreamBuffer(io.RawIOBase):
    """A live byte stream with file-like read semantics.

    - ``write()`` may be called from any thread.
    - ``read()``/``readinto()`` block until data is available or EOF.
    - Bounded to ``max_bytes``: when the consumer stalls, the *oldest* data is
      dropped so memory stays flat and playback resumes near-live.
    - ``finish()`` marks EOF: pending/future reads return ``b""`` cleanly.
    """

    def __init__(self, max_bytes: int = 4 * 1024 * 1024, align: int = 1) -> None:
        super().__init__()
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if align < 1:
            raise ValueError("align must be positive")
        self._chunks: Deque[bytes] = collections.deque()
        self._size = 0
        self._max = max_bytes
        # Frame size, for streams where a partial drop would corrupt every
        # later sample: dropping a non-multiple of it from raw PCM shifts
        # the channel interleave for the rest of the session.
        self._align = align
        self._cond = threading.Condition()
        self._eof = False
        self.dropped_bytes = 0

    # ------------------------------------------------------------------ producer
    def write(self, b: bytes) -> int:  # type: ignore[override]
        if not b:
            return 0
        with self._cond:
            if self._eof:
                raise ValueError("write to finished StreamBuffer")
            self._chunks.append(bytes(b))
            self._size += len(b)
            while self._size > self._max and self._chunks:
                dropped = self._chunks.popleft()
                self._size -= len(dropped)
                self.dropped_bytes += len(dropped)
            self._realign()
            self._cond.notify_all()
        return len(b)

    def _realign(self) -> None:
        """Drop a few more bytes if needed so the stream stays frame-aligned."""
        extra = (-self.dropped_bytes) % self._align
        while extra and self._chunks:
            head = self._chunks[0]
            if len(head) <= extra:
                self._chunks.popleft()
                self._size -= len(head)
                self.dropped_bytes += len(head)
                extra -= len(head)
            else:
                self._chunks[0] = head[extra:]
                self._size -= extra
                self.dropped_bytes += extra
                extra = 0

    def trim_oldest(self, nbytes: int) -> int:
        """Discard up to ``nbytes`` of the oldest audio; returns bytes freed.

        Used to pull a lagging device back to the live edge so several
        speakers stay in sync - the backlog standing in each buffer IS that
        device's playback offset."""
        if nbytes <= 0:
            return 0
        with self._cond:
            before = self.dropped_bytes
            freed, target = 0, min(nbytes, self._size)
            while freed < target and self._chunks:
                head = self._chunks[0]
                if len(head) <= target - freed:
                    self._chunks.popleft()
                    self._size -= len(head)
                    freed += len(head)
                else:
                    take = target - freed
                    self._chunks[0] = head[take:]
                    self._size -= take
                    freed += take
            self.dropped_bytes += freed
            self._realign()  # may discard a few more to keep frame alignment
            return self.dropped_bytes - before

    def finish(self) -> None:
        """Mark end-of-stream. Readers drain remaining data, then get EOF."""
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    # ------------------------------------------------------------------ consumer
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def _wait_for_data(self) -> bool:
        """Block until a chunk exists or EOF. Return True if data available."""
        while not self._chunks and not self._eof:
            self._cond.wait(timeout=0.5)
        return bool(self._chunks)

    def readinto(self, b) -> int:  # type: ignore[override]
        with self._cond:
            if not self._wait_for_data():
                return 0  # EOF
            chunk = self._chunks.popleft()
            n = min(len(chunk), len(b))
            b[:n] = chunk[:n]
            if n < len(chunk):
                self._chunks.appendleft(chunk[n:])
            self._size -= n
            return n

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        if size is None or size < 0:
            # "Read all" would never return on a live stream; hand back the
            # next available chunk instead.
            with self._cond:
                if not self._wait_for_data():
                    return b""
                chunk = self._chunks.popleft()
                self._size -= len(chunk)
                return chunk
        buf = bytearray(size)
        n = self.readinto(memoryview(buf))
        return bytes(buf[:n])

    def close(self) -> None:
        self.finish()
        super().close()
