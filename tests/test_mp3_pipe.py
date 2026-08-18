from homepod_bridge.mp3_pipe import Mp3Encoder, Mp3Pipe, SinkSwitch


class FakeEncoder:
    """Deterministic stand-in: prefixes payloads, fixed flush tail."""

    def encode(self, pcm: bytes) -> bytes:
        return b"E:" + pcm

    def flush(self) -> bytes:
        return b"TAIL"


def test_pipe_encodes_and_finishes_with_eof():
    pipe = Mp3Pipe(FakeEncoder())
    pipe.feed_pcm(b"aa")
    pipe.feed_pcm(b"bb")
    pipe.finish()
    reader = pipe.reader()
    assert reader.read() == b"E:aaE:bbTAIL"
    assert reader.read() == b""


def test_feed_after_finish_is_ignored():
    pipe = Mp3Pipe(FakeEncoder())
    pipe.finish()
    pipe.feed_pcm(b"late")  # must not raise or emit
    assert pipe.reader().read() == b"TAIL"


def test_feed_pcm_survives_buffer_finished_externally():
    """pyatv's dropped reader closes the buffer via GC at an arbitrary time;
    the resulting write-to-finished ValueError must never escape onto the
    capture thread."""
    pipe = Mp3Pipe(FakeEncoder())
    pipe.buffer.finish()  # closed underneath the pipe, not via pipe.finish()
    pipe.feed_pcm(b"aa")  # must not raise
    pipe.finish()  # tail write is likewise guarded


def test_double_finish_is_idempotent():
    pipe = Mp3Pipe(FakeEncoder())
    pipe.finish()
    pipe.finish()
    assert pipe.reader().read() == b"TAIL"


def test_sink_switch_rewires_between_pipes():
    switch = SinkSwitch()
    a, b = Mp3Pipe(FakeEncoder()), Mp3Pipe(FakeEncoder())

    switch(b"dropped")  # no sink yet: silently discarded
    switch.set(a.feed_pcm)
    switch(b"one")
    switch.set(b.feed_pcm)
    switch(b"two")

    a.finish()
    b.finish()
    assert a.reader().read() == b"E:oneTAIL"
    assert b.reader().read() == b"E:twoTAIL"


def test_real_lame_encoder_produces_mp3_frames():
    # 200ms of stereo int16 silence @ 48kHz, as the capture thread would emit.
    enc = Mp3Encoder(sample_rate=48000, channels=2, bitrate_kbps=320)
    pcm = b"\x00\x00" * 2 * 9600
    out = enc.encode(pcm) + enc.flush()
    assert len(out) > 0
    assert b"\xff" in out  # MP3 frame sync byte present
