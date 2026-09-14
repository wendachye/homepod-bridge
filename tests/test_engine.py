import asyncio
import time

import pytest
from pyatv.const import PairingRequirement

from homepod_bridge.airplay import DeviceInfo
from homepod_bridge.capture import CaptureFormat
from homepod_bridge.engine import BridgeEngine, EngineState


def wait_until(pred, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def dev(name: str) -> DeviceInfo:
    return DeviceInfo(
        name=name,
        address="10.0.0.9",
        identifier=f"id-{name}",
        raop_pairing=PairingRequirement.NotNeeded,
    )


class FakeCapture:
    def __init__(self, sink, on_failure=None):
        self.sink = sink
        self.on_failure = on_failure
        self.fmt = CaptureFormat(sample_rate=48000, channels=2)
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeEncoder:
    def encode(self, pcm):
        return b"E:" + pcm

    def flush(self):
        return b"|"


class FakeAtv:
    def __init__(self):
        self.levels = []
        outer = self

        class _Audio:
            async def set_volume(self, level, output_device=None):
                outer.levels.append(level)

        self.audio = _Audio()


async def fake_stream(identifier, open_reader, *, policy, stop_event, stats, on_event):
    """Connects immediately, streams until the engine stops the session."""
    fake_stream.events[identifier] = on_event  # let tests drive reconnects
    on_event("connecting", None)
    # Retain the reader like pyatv would: dropping it lets GC close the
    # underlying StreamBuffer, which makes buffer tests race the collector.
    fake_stream.readers[identifier] = open_reader()
    atv = FakeAtv()
    fake_stream.atvs.setdefault(identifier, []).append(atv)
    on_event("connected", atv)
    try:
        await stop_event.wait()
    finally:
        on_event("disconnected", None)


fake_stream.events = {}
fake_stream.readers = {}


@pytest.fixture
def env():
    fake_stream.atvs = {}
    captures = []

    def capture_factory(sink, on_failure=None):
        cap = FakeCapture(sink, on_failure)
        captures.append(cap)
        return cap

    async def scan_fn(timeout=6):
        return [dev("Living Room"), dev("Living Room (2)"), dev("Master Bedroom")]

    engine = BridgeEngine(
        scan_fn=scan_fn,
        stream_fn=fake_stream,
        capture_factory=capture_factory,
        encoder_factory=lambda fmt: FakeEncoder(),
        volume=40.0,
    )
    try:
        yield engine, captures
    finally:
        engine.shutdown()


def test_full_lifecycle(env):
    engine, captures = env
    snap = engine.rescan()
    assert len(snap.devices) == 3
    assert snap.state is EngineState.IDLE

    engine.set_selected(["id-Living Room", "id-Living Room (2)"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    snap = engine.snapshot()
    assert snap.connected == ("id-Living Room", "id-Living Room (2)")
    assert captures[0].started and not captures[0].stopped

    snap = engine.stop_streaming()
    assert snap.state is EngineState.IDLE
    assert snap.connected == ()
    assert captures[0].stopped
    assert snap.selected == ("id-Living Room", "id-Living Room (2)")  # selection survives


def test_volume_applied_on_connect_and_on_change(env):
    engine, _ = env
    engine.rescan()
    engine.set_selected(["id-Master Bedroom"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)

    atv = fake_stream.atvs["id-Master Bedroom"][0]
    assert wait_until(lambda: atv.levels[:1] == [40.0])  # initial volume pushed

    snap = engine.set_volume(75.0)
    assert snap.volume == 75.0
    assert atv.levels[-1] == 75.0

    snap = engine.nudge_volume(+50.0)  # clamps at 100
    assert snap.volume == 100.0
    assert atv.levels[-1] == 100.0


def test_toggle_while_streaming_restarts_session(env):
    engine, captures = env
    engine.rescan()
    engine.set_selected(["id-Living Room"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().connected == ("id-Living Room",))

    snap = engine.toggle_device("id-Living Room (2)")
    assert set(snap.selected) == {"id-Living Room", "id-Living Room (2)"}
    assert wait_until(
        lambda: engine.snapshot().connected == ("id-Living Room", "id-Living Room (2)")
    )
    assert len(captures) == 2  # old session torn down, new capture created
    assert captures[0].stopped and captures[1].started

    # toggling the last device off ends the session entirely
    engine.toggle_device("id-Living Room")
    snap = engine.toggle_device("id-Living Room (2)")
    assert snap.selected == ()
    assert snap.state is EngineState.IDLE
    assert captures[-1].stopped


def test_toggle_while_idle_only_updates_selection(env):
    engine, captures = env
    engine.rescan()
    snap = engine.toggle_device("id-Master Bedroom")
    assert snap.selected == ("id-Master Bedroom",)
    assert snap.state is EngineState.IDLE
    assert captures == []  # nothing started


def test_on_change_fires_with_snapshots():
    fake_stream.atvs = {}
    seen = []

    async def scan_fn(timeout=6):
        return [dev("Living Room")]

    engine = BridgeEngine(
        scan_fn=scan_fn,
        stream_fn=fake_stream,
        capture_factory=FakeCapture,
        encoder_factory=lambda fmt: FakeEncoder(),
        on_change=seen.append,
    )
    try:
        engine.rescan()
        engine.set_selected(["id-Living Room"])
        engine.start_selected()
        assert wait_until(lambda: any(s.state is EngineState.STREAMING for s in seen))
        engine.stop_streaming()
        assert seen[-1].state is EngineState.IDLE
    finally:
        engine.shutdown()


def test_device_volume_override_applied_on_connect():
    fake_stream.atvs = {}

    async def scan_fn(timeout=6):
        return [dev("Living Room"), dev("Living Room (2)")]

    engine = BridgeEngine(
        scan_fn=scan_fn,
        stream_fn=fake_stream,
        capture_factory=FakeCapture,
        encoder_factory=lambda fmt: FakeEncoder(),
        volume=40.0,
        device_volumes={"id-Living Room": 20.0},
    )
    try:
        engine.rescan()
        engine.set_selected(["id-Living Room", "id-Living Room (2)"])
        engine.start_selected()
        assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
        lr = fake_stream.atvs["id-Living Room"][0]
        lr2 = fake_stream.atvs["id-Living Room (2)"][0]
        assert wait_until(lambda: lr.levels[:1] == [20.0])  # its own override
        assert wait_until(lambda: lr2.levels[:1] == [40.0])  # master default
        snap = engine.snapshot()
        assert snap.volumes == (("id-Living Room", 20.0), ("id-Living Room (2)", 40.0))
        assert snap.volume == 40.0  # master untouched by the override
    finally:
        engine.shutdown()


def test_set_device_volume_targets_only_that_device():
    fake_stream.atvs = {}

    async def scan_fn(timeout=6):
        return [dev("Living Room"), dev("Living Room (2)")]

    engine = BridgeEngine(
        scan_fn=scan_fn,
        stream_fn=fake_stream,
        capture_factory=FakeCapture,
        encoder_factory=lambda fmt: FakeEncoder(),
        volume=40.0,
    )
    try:
        engine.rescan()
        engine.set_selected(["id-Living Room", "id-Living Room (2)"])
        engine.start_selected()
        assert wait_until(lambda: engine.snapshot().connected != ())
        lr = fake_stream.atvs["id-Living Room"][0]
        lr2 = fake_stream.atvs["id-Living Room (2)"][0]
        assert wait_until(lambda: lr.levels and lr2.levels)

        snap = engine.set_device_volume("id-Living Room", 65.0)
        assert lr.levels[-1] == 65.0
        assert lr2.levels[-1] == 40.0  # untouched
        assert snap.volumes == (("id-Living Room", 65.0), ("id-Living Room (2)", 40.0))

        # Master afterwards: sets EVERYTHING and clears the override.
        snap = engine.set_volume(80.0)
        assert lr.levels[-1] == 80.0 and lr2.levels[-1] == 80.0
        assert snap.volumes == (("id-Living Room", 80.0), ("id-Living Room (2)", 80.0))
    finally:
        engine.shutdown()


def test_overlapping_volume_sends_land_in_submission_order(env):
    """A laggy device apply must never let an older level land after a newer
    one - the per-device worker drains to the LATEST queued value."""
    engine, _captures = env
    engine.rescan()
    engine.set_selected(["id-Master Bedroom"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    atv = fake_stream.atvs["id-Master Bedroom"][0]
    assert wait_until(lambda: atv.levels)  # initial push landed

    class SlowAudio:
        def __init__(self, sink):
            self._sink = sink

        async def set_volume(self, level, output_device=None):
            await asyncio.sleep(0.05)
            self._sink.append(level)

    atv.audio = SlowAudio(atv.levels)
    engine.set_device_volume_nowait("id-Master Bedroom", 30.0)
    engine.set_device_volume_nowait("id-Master Bedroom", 80.0)
    assert wait_until(lambda: 80.0 in atv.levels)
    time.sleep(0.15)  # give any straggler a chance to land out of order
    assert atv.levels[-1] == 80.0


def test_default_wire_format_is_raw_pcm():
    """pyatv sends PCM to the device regardless, and its decoder init stalls
    ~1.6s on an MP3 stream (permanent, since RAOP never catches up). The
    production default must therefore be a WAV/PCM stream."""
    from homepod_bridge.capture import CaptureFormat
    from homepod_bridge.pcm_pipe import PcmPipe

    engine = BridgeEngine(  # no encoder_factory: the production path
        scan_fn=_scan_one,
        stream_fn=fake_stream,
        capture_factory=FakeCapture,
    )
    try:
        pipe = engine._make_pipe(CaptureFormat(sample_rate=48000, channels=2))
        assert isinstance(pipe, PcmPipe)
        header = pipe.reader().read(44)
        assert header[:4] == b"RIFF" and header[8:12] == b"WAVE"
        assert pipe.buffer._align == 4  # drops must not break frame alignment
        # bound follows the PCM byte rate, not a bitrate
        from homepod_bridge import engine as engine_mod

        assert pipe.buffer._max == int(48000 * 2 * 2 * engine_mod.LIVE_BUFFER_SECONDS)
    finally:
        engine.shutdown()


def test_live_buffer_is_latency_bounded_not_memory_bounded(env):
    """The RAOP consumer drains at realtime only, so buffered backlog IS
    added latency. The old 4MB byte bound allowed ~105s of creeping delay;
    the live bound must be a few SECONDS of audio at the session bitrate."""
    from homepod_bridge import engine as engine_mod

    engine, _ = env  # default bitrate: 320kbps
    engine.rescan()
    engine.set_selected(["id-Living Room"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)

    byte_rate = 320_000 // 8
    pipe = engine._pipes["id-Living Room"]
    # seconds-of-audio bound, with a 64KiB floor so the buffer can always
    # satisfy pyatv's decoder-init read
    cap = max(64 * 1024, int(byte_rate * engine_mod.LIVE_BUFFER_SECONDS))
    assert pipe.buffer._max == cap
    assert cap / byte_rate <= 2.0  # nothing like the old ~105s

    chunk = b"x" * 850  # ~21ms of 320kbps MP3
    for _ in range(int(10 * byte_rate / len(chunk))):  # 10s stall, no reader
        pipe.buffer.write(chunk)
    assert pipe.buffer._size <= cap  # backlog cannot grow past the bound
    assert pipe.buffer.dropped_bytes > 0  # oldest audio went overboard


def test_reconnect_repushes_volume_after_hung_apply(env):
    """A volume apply hung on a dying connection must not swallow the
    reconnect's volume push: the worker's done-check compares the CONNECTION
    identity, not just the level, or the fresh session would stream at
    pyatv's default while the UI reports the user's level."""
    engine, _ = env
    engine.rescan()
    engine.set_selected(["id-Master Bedroom"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    atv1 = fake_stream.atvs["id-Master Bedroom"][0]
    assert wait_until(lambda: atv1.levels)  # initial push landed

    class HangingAudio:
        def __init__(self):
            self.gate = None
            self.calls = []

        async def set_volume(self, level, output_device=None):
            self.calls.append(level)
            if self.gate is None:
                self.gate = asyncio.Event()
            await self.gate.wait()

    hung = HangingAudio()
    atv1.audio = hung
    # Same level as already effective: the reconnect will re-queue this
    # exact value, which a value-only done-check would treat as delivered.
    engine.set_device_volume_nowait("id-Master Bedroom", 40.0)
    assert wait_until(lambda: hung.calls == [40.0])  # apply is now hanging

    on_event = fake_stream.events["id-Master Bedroom"]
    atv2 = FakeAtv()
    engine._loop.call_soon_threadsafe(on_event, "disconnected", None)
    engine._loop.call_soon_threadsafe(on_event, "connected", atv2)
    assert wait_until(lambda: engine.snapshot().connected == ("id-Master Bedroom",))
    engine._loop.call_soon_threadsafe(lambda: hung.gate.set())  # stale apply ends

    assert wait_until(lambda: atv2.levels == [40.0])  # fresh session got it


def test_blocking_volume_call_survives_concurrent_stop(env):
    """stop_streaming cancels the volume workers; a blocking set_volume
    waiting on one must still return a Snapshot, not raise CancelledError."""
    from concurrent.futures import ThreadPoolExecutor

    engine, _ = env
    engine.rescan()
    engine.set_selected(["id-Master Bedroom"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    atv = fake_stream.atvs["id-Master Bedroom"][0]
    assert wait_until(lambda: atv.levels)

    class SlowAudio:
        async def set_volume(self, level, output_device=None):
            await asyncio.sleep(0.3)

    atv.audio = SlowAudio()
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(engine.set_volume, 75.0)
        time.sleep(0.1)  # let it queue the worker and block in _drained
        engine.stop_streaming()
        snap = fut.result(timeout=5)
    assert snap.volume == 75.0  # a Snapshot came back despite the stop


class BrokenStopCapture(FakeCapture):
    """PyAudio commonly raises from stop_stream()/close() when the device
    just died - exactly the case that triggers recovery."""

    def stop(self):
        super().stop()
        raise OSError("device vanished")


class FailingStartCapture(FakeCapture):
    def start(self):
        raise RuntimeError("device busy")


class SurroundCapture(FakeCapture):
    def __init__(self, sink, on_failure=None):
        super().__init__(sink, on_failure)
        self.fmt = CaptureFormat(sample_rate=48000, channels=6)


async def _scan_one(timeout=6):
    return [dev("Living Room")]


def make_engine(capture_factory, **kwargs):
    fake_stream.atvs = {}
    return BridgeEngine(
        scan_fn=_scan_one,
        stream_fn=fake_stream,
        capture_factory=capture_factory,
        encoder_factory=lambda fmt: FakeEncoder(),
        **kwargs,
    )


def test_stop_survives_capture_stop_failure():
    captures = []

    def factory(sink, on_failure=None):
        cap = BrokenStopCapture(sink, on_failure)
        captures.append(cap)
        return cap

    engine = make_engine(factory)
    try:
        engine.rescan()
        engine.set_selected(["id-Living Room"])
        engine.start_selected()
        assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)

        snap = engine.stop_streaming()  # capture.stop raises mid-teardown
        assert snap.state is EngineState.IDLE  # state reset must still run
        assert snap.connected == ()

        engine.start_selected()  # and the engine must still be usable
        assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    finally:
        engine.shutdown()


def test_failed_capture_start_cleans_up_and_next_connect_works():
    captures = []

    def factory(sink, on_failure=None):
        cls = FailingStartCapture if not captures else FakeCapture
        cap = cls(sink, on_failure)
        captures.append(cap)
        return cap

    engine = make_engine(factory)
    try:
        engine.rescan()
        engine.set_selected(["id-Living Room"])
        with pytest.raises(RuntimeError, match="device busy"):
            engine.start_selected()
        assert engine.snapshot().state is EngineState.IDLE
        assert captures[0].stopped  # the half-started capture was not leaked

        engine.start_selected()
        assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    finally:
        engine.shutdown()


def test_surround_output_is_rejected_with_alert():
    captures = []
    alerts = []

    def factory(sink, on_failure=None):
        cap = SurroundCapture(sink, on_failure)
        captures.append(cap)
        return cap

    engine = make_engine(factory, on_alert=alerts.append)
    try:
        engine.rescan()
        engine.set_selected(["id-Living Room"])
        snap = engine.start_selected()
        assert snap.state is EngineState.IDLE  # rejected cleanly, no flapping
        assert captures[0].stopped
        assert alerts and "6-channel" in alerts[0]
    finally:
        engine.shutdown()


def test_shutdown_stops_loop_thread(env):
    engine, _ = env
    engine.rescan()
    engine.set_selected(["id-Living Room"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    engine.shutdown()
    assert not engine._thread.is_alive()
    engine.shutdown()  # idempotent


def test_duplicate_names_keep_selection_streams_and_volume_isolated():
    from dataclasses import replace

    fake_stream.atvs = {}
    first = replace(dev("Bedroom"), identifier="speaker-1", address="10.0.0.1")
    second = replace(dev("Bedroom"), identifier="speaker-2", address="10.0.0.2")
    devices = [first, second]

    async def scan_fn(timeout=6):
        return list(devices)

    engine = BridgeEngine(scan_fn=scan_fn, stream_fn=fake_stream,
                          capture_factory=FakeCapture,
                          encoder_factory=lambda fmt: FakeEncoder(),
                          device_volumes={"speaker-1": 20.0, "speaker-2": 70.0})
    try:
        engine.rescan()
        engine.set_selected(["speaker-1"])
        engine.start_selected()
        assert wait_until(lambda: engine.snapshot().connected == ("speaker-1",))
        assert set(fake_stream.atvs) == {"speaker-1"}

        engine.set_selected(["speaker-1", "speaker-2", "speaker-1"])
        assert wait_until(lambda: engine.snapshot().connected == ("speaker-1", "speaker-2"))
        assert set(engine._pipes) == {"speaker-1", "speaker-2"}
        assert engine._pipes["speaker-1"] is not engine._pipes["speaker-2"]
        assert engine.snapshot().volumes == (("speaker-1", 20.0), ("speaker-2", 70.0))
        first_atv, second_atv = fake_stream.atvs["speaker-1"][-1], fake_stream.atvs["speaker-2"][-1]
        assert wait_until(lambda: first_atv.levels and second_atv.levels)
        engine.set_device_volume("speaker-1", 35.0)
        assert first_atv.levels[-1] == 35.0 and second_atv.levels[-1] == 70.0

        # Device identity survives renames and changes in discovery order.
        devices[:] = [second, replace(first, name="Guest Bedroom")]
        snap = engine.rescan()
        assert snap.selected == ("speaker-1", "speaker-2")
        assert snap.device_label("speaker-1") == "Guest Bedroom"
        assert snap.volumes == (("speaker-1", 35.0), ("speaker-2", 70.0))
    finally:
        engine.shutdown()


def test_missing_identifier_never_falls_back_to_a_matching_display_name(env):
    engine, captures = env
    engine.rescan()
    engine.set_selected(["Living Room"])
    assert engine.start_selected().state is EngineState.IDLE
    assert captures == []


def test_shutdown_cancels_an_inflight_scan():
    import concurrent.futures
    import threading

    scanning = threading.Event()
    cancelled = threading.Event()

    async def slow_scan(timeout=6):
        scanning.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    engine = BridgeEngine(scan_fn=slow_scan)
    with concurrent.futures.ThreadPoolExecutor() as executor:
        pending = executor.submit(engine.rescan)
        assert scanning.wait(2)
        engine.shutdown()
        assert cancelled.is_set()
        with pytest.raises(concurrent.futures.CancelledError):
            pending.result(timeout=2)


def test_slow_old_scan_does_not_overwrite_newer_discovery():
    import concurrent.futures
    import threading

    scanning = threading.Event()
    release = None
    calls = 0

    async def scanner(timeout=6):
        nonlocal calls, release
        calls += 1
        if calls == 1:
            release = asyncio.Event()
            scanning.set()
            await release.wait()
            return [dev("Old name")]
        return [dev("New name")]

    engine = BridgeEngine(scan_fn=scanner)
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            old_scan = executor.submit(engine.rescan)
            assert scanning.wait(2)
            assert engine.rescan().devices[0].name == "New name"
            engine._loop.call_soon_threadsafe(release.set)
            assert old_scan.result(timeout=2).devices[0].name == "New name"
            assert engine.snapshot().devices[0].name == "New name"
    finally:
        engine.shutdown()
