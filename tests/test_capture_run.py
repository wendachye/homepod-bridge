"""The capture thread's failure-detection wiring.

These drive the REAL ``LoopbackCapture._run`` / ``_watch_default_device``
loops (constructed via ``__new__`` so no audio hardware or PyAudioWPatch is
needed): before 0.6.0 a sink exception escaped ``_run`` past the
``on_failure`` logic, silently killing capture with the tray still green.
"""
import sys
import threading
import time

import pytest

from homepod_bridge.capture import (
    CHUNK_FRAMES,
    CaptureFormat,
    LoopbackCapture,
    _default_render_endpoint_id,
)


class FakeStream:
    """Always has a full chunk ready (the 'audio is playing' case)."""

    def __init__(self, available=10_000):
        self.chunk = b"\x00" * 64
        self._available = available

    def get_read_available(self):
        return self._available

    def read(self, frames, exception_on_overflow=False):
        time.sleep(0.001)
        return self.chunk


class SilentStream(FakeStream):
    """WASAPI loopback during silence: never any frames to read."""

    def __init__(self):
        super().__init__(available=0)

    def read(self, frames, exception_on_overflow=False):  # pragma: no cover
        raise AssertionError("must not read when nothing is available")


def bare_capture(sink, on_failure, probe=None, poll=0.01, stream=None):
    cap = LoopbackCapture.__new__(LoopbackCapture)  # skip device discovery
    cap.fmt = CaptureFormat(sample_rate=48000, channels=2)
    cap._sink = sink
    cap._on_failure = on_failure
    cap._stop = threading.Event()
    cap._stream = stream if stream is not None else FakeStream()
    cap._thread = None
    cap._watcher = None
    cap._device_changed = False
    cap._poll_interval = poll
    cap._probe = probe if probe is not None else (lambda: None)
    cap._baseline_ref = "endpoint-A"
    return cap


def test_sink_exception_fires_on_failure():
    """The v0.5.x hole: encoder/buffer errors killed the thread silently."""
    failures = []

    def sink(_data):
        raise ValueError("write to finished StreamBuffer")

    cap = bare_capture(sink, lambda: failures.append(1))
    cap._run()  # must return, not raise
    assert failures == [1]


def test_silence_is_injected_so_the_raop_stream_never_starves():
    """WASAPI loopback yields no frames while the machine is silent. Without
    injected silence the encoder stalls and HomePods drop the session."""
    chunks = []
    cap = bare_capture(chunks.append, None, stream=SilentStream())
    thread = threading.Thread(target=cap._run, daemon=True)
    thread.start()
    time.sleep(0.25)  # ~11 chunk periods @ 21.3ms
    cap._stop.set()
    thread.join(1.0)
    assert not thread.is_alive()
    expected = CHUNK_FRAMES * 2 * 2  # frames * channels * int16
    assert chunks, "no silence was fed while the device was quiet"
    assert all(c == b"\x00" * expected for c in chunks)
    # Paced at roughly real time, not spun as fast as the CPU allows.
    assert 4 <= len(chunks) <= 25, f"unexpected pacing: {len(chunks)} chunks"


class BurstyStream(FakeStream):
    """Real WASAPI behaviour: 480-frame packets every 10ms, so a full
    1024-frame chunk is ready only every 20-30ms, not on a fixed period."""

    def __init__(self):
        super().__init__()
        self._t0 = time.monotonic()
        self._delivered = 0

    def get_read_available(self):
        elapsed = time.monotonic() - self._t0
        packets = int(elapsed / 0.010) * 480  # WASAPI packet cadence
        return max(0, packets - self._delivered)

    def read(self, frames, exception_on_overflow=False):
        self._delivered += frames
        return b"\x01" * (frames * 2 * 2)


def test_playback_is_not_over_produced_with_phantom_silence():
    """The 0.7.1 regression: silence was injected on a per-chunk deadline,
    so every longer-than-average WASAPI gap mid-playback added a chunk
    nothing asked for - 13% more audio than realtime. RAOP drains at
    realtime only, so the excess becomes permanent latency then dropouts."""
    produced = []
    cap = bare_capture(produced.append, None, stream=BurstyStream())
    thread = threading.Thread(target=cap._run, daemon=True)
    t0 = time.monotonic()
    thread.start()
    time.sleep(3.0)
    cap._stop.set()
    thread.join(2.0)
    elapsed = time.monotonic() - t0

    frames = sum(len(c) for c in produced) / 4  # bytes -> frames (stereo s16)
    audio_seconds = frames / 48000
    ratio = audio_seconds / elapsed
    assert 0.95 <= ratio <= 1.05, (
        f"produced {ratio:.3f}x realtime ({audio_seconds:.2f}s of audio in "
        f"{elapsed:.2f}s) - phantom silence inflates latency"
    )
    silent = [c for c in produced if c == b"\x00" * len(c)]
    assert len(silent) <= 2, f"{len(silent)} silence chunks injected mid-playback"


def test_silent_capture_stops_promptly():
    """The 2s join timeout used to expire on every disconnect with audio
    paused, forcing the deliberate-leak path."""
    cap = bare_capture(lambda d: None, None, stream=SilentStream())
    thread = threading.Thread(target=cap._run, daemon=True)
    cap._thread = thread
    cap._watcher = None
    thread.start()
    time.sleep(0.05)
    t0 = time.perf_counter()
    cap._stop.set()
    thread.join(1.0)
    assert not thread.is_alive()
    assert time.perf_counter() - t0 < 0.3  # no blocking read to wait out


def test_read_exception_fires_on_failure():
    failures = []
    cap = bare_capture(lambda d: None, lambda: failures.append(1))

    def bad_read(frames, exception_on_overflow=False):
        raise OSError("device gone")

    cap._stream.read = bad_read
    cap._run()
    assert failures == [1]


def test_failure_during_stop_is_not_reported():
    failures = []
    cap = bare_capture(lambda d: None, lambda: failures.append(1))

    def bad_read(frames, exception_on_overflow=False):
        cap._stop.set()  # stop() closed the stream under the read
        raise OSError("stream closed")

    cap._stream.read = bad_read
    cap._run()
    assert failures == []


def test_default_device_change_triggers_failure():
    failures = []
    done = threading.Event()

    def on_failure():
        failures.append(1)
        done.set()

    cap = bare_capture(lambda d: None, on_failure, probe=lambda: "endpoint-B")
    reader = threading.Thread(target=cap._run, daemon=True)
    watcher = threading.Thread(target=cap._watch_default_device, daemon=True)
    reader.start()
    watcher.start()
    assert done.wait(2.0), "device switch never reached on_failure"
    cap._stop.set()
    reader.join(1.0)
    watcher.join(1.0)
    assert cap._device_changed is True
    assert failures == [1]


def test_unchanged_device_never_triggers():
    cap = bare_capture(lambda d: None, None, probe=lambda: "endpoint-A", poll=0.005)
    watcher = threading.Thread(target=cap._watch_default_device, daemon=True)
    watcher.start()
    time.sleep(0.05)
    cap._stop.set()
    watcher.join(1.0)
    assert cap._device_changed is False


def test_device_vanishing_and_returning_triggers_restart():
    """The monitor is often the default output; when the display sleeps
    Windows removes its HDMI audio endpoint and restores it with the SAME
    id on wake. Comparing ids alone sees nothing, so the bridge kept a
    stale stream and fed silence forever."""
    from homepod_bridge.capture import DEVICE_MISSING_POLLS

    seq = ["endpoint-A"] + [None] * (DEVICE_MISSING_POLLS + 1) + ["endpoint-A"] * 5
    calls = {"i": 0}

    def probe():
        i = calls["i"]
        calls["i"] += 1
        return seq[i] if i < len(seq) else "endpoint-A"

    cap = bare_capture(lambda d: None, None, probe=probe, poll=0.005)
    watcher = threading.Thread(target=cap._watch_default_device, daemon=True)
    watcher.start()
    watcher.join(2.0)
    assert cap._device_changed is True, "device returning after an absence was missed"


def test_single_probe_hiccup_does_not_restart():
    """One failed COM probe is noise, not a device change."""
    seq = ["endpoint-A", None, "endpoint-A", "endpoint-A", "endpoint-A"]
    calls = {"i": 0}

    def probe():
        i = calls["i"]
        calls["i"] += 1
        return seq[i] if i < len(seq) else "endpoint-A"

    cap = bare_capture(lambda d: None, None, probe=probe, poll=0.005)
    watcher = threading.Thread(target=cap._watch_default_device, daemon=True)
    watcher.start()
    time.sleep(0.1)
    cap._stop.set()
    watcher.join(1.0)
    assert cap._device_changed is False


def test_probe_errors_do_not_trigger_failure():
    cap = bare_capture(lambda d: None, None, probe=lambda: None, poll=0.005)
    watcher = threading.Thread(target=cap._watch_default_device, daemon=True)
    watcher.start()
    time.sleep(0.05)
    cap._stop.set()
    watcher.join(1.0)
    assert cap._device_changed is False


def test_lazy_baseline_when_start_probe_failed():
    """Probe failed at start(): the first successful poll anchors the
    baseline instead of triggering a bogus restart."""
    cap = bare_capture(lambda d: None, None, probe=lambda: "endpoint-B", poll=0.005)
    cap._baseline_ref = None
    watcher = threading.Thread(target=cap._watch_default_device, daemon=True)
    watcher.start()
    time.sleep(0.05)
    cap._stop.set()
    watcher.join(1.0)
    assert cap._device_changed is False


def test_real_endpoint_probe_is_stable_and_never_raises():
    """Exercises the actual Core Audio COM plumbing. On a machine with no
    audio endpoint (CI) it must return None from BOTH calls, not raise;
    PortAudio cannot be used here because its device table is frozen
    process-wide while a capture instance is alive."""
    if sys.platform != "win32":
        pytest.skip("Core Audio COM probe is Windows-only")
    first = _default_render_endpoint_id()
    second = _default_render_endpoint_id()
    assert first == second  # stable ID (or None/None on audio-less CI)
