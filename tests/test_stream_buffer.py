import io
import threading
import time

import pytest

from homepod_bridge.stream_buffer import StreamBuffer


def test_trim_oldest_pulls_the_stream_toward_live():
    """Multi-room sync: the backlog standing in a device's buffer IS how far
    behind live it plays, so trimming it pulls that room forward."""
    buf = StreamBuffer(max_bytes=10_000)
    buf.write(b"a" * 400)
    buf.write(b"b" * 400)
    assert buf.trim_oldest(500) == 500
    assert buf._size == 300
    assert buf.read() == b"b" * 300  # oldest audio went, newest survives
    assert buf.dropped_bytes == 500


def test_trim_oldest_is_frame_aligned_and_bounded():
    buf = StreamBuffer(max_bytes=10_000, align=4)
    buf.write(b"x" * 1000)
    freed = buf.trim_oldest(333)  # unaligned request
    assert freed >= 333 and freed % 4 == 0  # rounded up to a frame boundary
    assert buf.dropped_bytes % 4 == 0
    assert buf._size == 1000 - freed

    assert buf.trim_oldest(0) == 0
    assert buf.trim_oldest(999_999) == 1000 - freed  # never over-trims
    assert buf._size == 0


def test_reads_in_order():
    buf = StreamBuffer()
    buf.write(b"abc")
    buf.write(b"def")
    assert buf.read(3) == b"abc"
    assert buf.read(3) == b"def"


def test_partial_read_splits_chunk():
    buf = StreamBuffer()
    buf.write(b"abcdef")
    assert buf.read(4) == b"abcd"
    assert buf.read(4) == b"ef"


def test_read_all_returns_next_chunk_not_blocking_forever():
    buf = StreamBuffer()
    buf.write(b"live-data")
    # size=-1 on a live stream must return promptly with available data
    assert buf.read(-1) == b"live-data"


def test_finish_gives_clean_eof_after_draining():
    buf = StreamBuffer()
    buf.write(b"tail")
    buf.finish()
    assert buf.read(10) == b"tail"
    assert buf.read(10) == b""
    assert buf.read(-1) == b""


def test_write_after_finish_raises():
    buf = StreamBuffer()
    buf.finish()
    with pytest.raises(ValueError):
        buf.write(b"x")


def test_bounded_drops_oldest():
    buf = StreamBuffer(max_bytes=10)
    buf.write(b"a" * 6)
    buf.write(b"b" * 6)  # exceeds cap -> oldest chunk dropped
    assert buf.dropped_bytes == 6
    assert buf.read(10) == b"b" * 6


def test_blocking_read_wakes_on_cross_thread_write():
    buf = StreamBuffer()
    result = {}

    def consumer():
        result["data"] = buf.read(4)

    t = threading.Thread(target=consumer)
    t.start()
    time.sleep(0.05)  # ensure consumer is blocked waiting
    buf.write(b"ping")
    t.join(timeout=2)
    assert not t.is_alive()
    assert result["data"] == b"ping"


def test_works_behind_buffered_reader_like_pyatv():
    # pyatv's stream_file takes io.BufferedIOBase; verify that path end to end.
    buf = StreamBuffer()
    reader = io.BufferedReader(buf)
    buf.write(b"x" * 100)
    buf.write(b"y" * 50)
    buf.finish()
    assert reader.read(100) == b"x" * 100
    assert reader.read(100) == b"y" * 50
    assert reader.read(100) == b""
    assert not reader.seekable()
