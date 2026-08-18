"""Live PCM/WAV wire format.

The container choice is worth a lot: pyatv's decoder init will not commit
until it has filled its 64 KiB buffer, so an MP3 stream stalls ~1.6s at
320kbps - and because RAOP paces at realtime and never catches up, that
stall is permanent session latency. Raw PCM opens in ~22ms.
"""
import struct

import pytest

from homepod_bridge.pcm_pipe import DECLARED_DATA_BYTES, PcmPipe, wav_header
from homepod_bridge.stream_buffer import StreamBuffer


def parse(header: bytes) -> dict:
    assert header[:4] == b"RIFF" and header[8:12] == b"WAVE"
    riff_size = struct.unpack("<I", header[4:8])[0]
    assert header[12:16] == b"fmt "
    (size, fmt, ch, rate, byte_rate, align, bits) = struct.unpack(
        "<IHHIIHH", header[16:36]
    )
    assert header[36:40] == b"data"
    return {
        "riff_size": riff_size,
        "fmt": fmt,
        "channels": ch,
        "rate": rate,
        "byte_rate": byte_rate,
        "align": align,
        "bits": bits,
        "data_size": struct.unpack("<I", header[40:44])[0],
    }


def test_header_describes_the_capture_format():
    h = wav_header(48000, 2, 2)
    assert len(h) == 44
    f = parse(h)
    assert f["fmt"] == 1  # PCM
    assert (f["channels"], f["rate"], f["bits"]) == (2, 48000, 16)
    assert f["byte_rate"] == 48000 * 2 * 2
    assert f["align"] == 4


def test_declared_length_avoids_the_scan_me_sentinel():
    """0xFFFFFFFF makes the parser scan the whole stream looking for the
    real end - measured stalling past 12s. Both size fields must stay clear
    of it while still declaring hours of audio."""
    f = parse(wav_header(48000, 2, 2))
    assert f["data_size"] != 0xFFFFFFFF
    assert f["riff_size"] != 0xFFFFFFFF
    assert f["data_size"] == DECLARED_DATA_BYTES
    hours = DECLARED_DATA_BYTES / (48000 * 2 * 2) / 3600
    assert hours > 6  # a session reconnects long before this


def test_pipe_emits_header_then_raw_frames():
    pipe = PcmPipe(48000, 2, 2)
    pipe.feed_pcm(b"\x01\x02" * 8)
    pipe.finish()
    data = pipe.reader().read()
    assert data[:4] == b"RIFF"
    assert data[44:] == b"\x01\x02" * 8  # payload is untouched PCM


def test_priming_starts_every_device_the_same_distance_from_live():
    """A receiver plays each sample at a time fixed by where its stream
    STARTED, so backlog present at the first read becomes that device's
    permanent offset. Devices connect seconds apart; priming is what puts
    them on one schedule."""
    rate = 48000 * 2 * 2
    early = PcmPipe(48000, 2, 2, buffer=StreamBuffer(max_bytes=rate, align=4),
                    prime_seconds=0.05)
    late = PcmPipe(48000, 2, 2, buffer=StreamBuffer(max_bytes=rate, align=4),
                   prime_seconds=0.05)
    early.feed_pcm(b"\x00" * int(rate * 0.02))  # connected promptly
    late.feed_pcm(b"\x00" * int(rate * 0.60))   # connected much later

    for pipe in (early, late):
        r = pipe.reader()
        r.read(44)  # header
        r.read(4)  # first audio read triggers priming
        assert pipe.buffer._size <= int(rate * 0.05) + 8

    # and priming only ever discards - it cannot add delay
    assert late.buffer.dropped_bytes > 0
    assert early.buffer.dropped_bytes == 0


def test_header_survives_buffer_overflow():
    """The bounded buffer drops its OLDEST bytes to stay live, and the
    header is by definition the oldest. Keeping it inside the buffer left
    pyatv reading unidentifiable raw PCM once ~1s of audio had arrived
    before its first read - which is exactly what the live path does."""
    cap = 48000 * 2 * 2 // 2  # 0.5s
    pipe = PcmPipe(48000, 2, 2, buffer=StreamBuffer(max_bytes=cap, align=4))
    for _ in range(47):  # ~1s of audio, overflowing the bound
        pipe.feed_pcm(b"\x11" * 4096)
    assert pipe.buffer.dropped_bytes > 0
    head = pipe.reader().read(44)
    assert head[:4] == b"RIFF" and head[8:12] == b"WAVE"


def test_drops_keep_pcm_frame_alignment():
    """A drop of a non-multiple of the frame size shifts the channel
    interleave for the rest of the session."""
    pipe = PcmPipe(48000, 2, 2, buffer=StreamBuffer(max_bytes=1000, align=4))
    for size in (333, 333, 777, 501):  # deliberately unaligned chunks
        pipe.feed_pcm(b"x" * size)
    assert pipe.buffer.dropped_bytes > 0
    assert pipe.buffer.dropped_bytes % 4 == 0


def test_reader_close_still_ends_the_stream():
    """pyatv drops the reader on disconnect; GC-closing it must finish the
    buffer so blocked readers wake (the engine's reconnect path relies on
    this)."""
    pipe = PcmPipe(48000, 2, 2)
    r = pipe.reader()
    r.close()
    assert pipe.buffer._eof is True


def test_header_is_readable_before_any_audio_arrives():
    """miniaudio must be able to identify the format immediately; waiting
    for the first capture chunk would reintroduce startup delay. The header
    is served from outside the buffer, so the buffer itself stays pure
    audio (and its bound stays a pure latency bound)."""
    pipe = PcmPipe(44100, 2, 2)
    assert pipe.buffer._size == 0
    assert pipe.reader().read(44) == wav_header(44100, 2, 2)


def test_feed_after_finish_is_ignored():
    pipe = PcmPipe(48000, 2, 2)
    pipe.finish()
    pipe.feed_pcm(b"late")
    assert pipe.reader().read() == wav_header(48000, 2, 2)


def test_feed_survives_buffer_finished_externally():
    """A dropped reader gets GC-closed at an arbitrary time; the resulting
    ValueError must never escape onto the shared capture thread."""
    pipe = PcmPipe(48000, 2, 2)
    pipe.buffer.finish()
    pipe.feed_pcm(b"\x00" * 16)  # must not raise


def test_respects_a_bounded_buffer():
    pipe = PcmPipe(48000, 2, 2, buffer=StreamBuffer(max_bytes=1024))
    for _ in range(100):
        pipe.feed_pcm(b"\x00" * 512)
    assert pipe.buffer._size <= 1024
    assert pipe.buffer.dropped_bytes > 0


@pytest.mark.parametrize("channels,rate", [(1, 44100), (2, 48000), (2, 96000)])
def test_header_round_trips_for_common_formats(channels, rate):
    f = parse(wav_header(rate, channels, 2))
    assert (f["channels"], f["rate"]) == (channels, rate)
    assert f["byte_rate"] == rate * channels * 2
