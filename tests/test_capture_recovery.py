"""Engine recovery when the audio capture dies (sleep/resume, device loss)."""
import time

import pytest

from homepod_bridge.engine import BridgeEngine, EngineState
from tests.test_engine import (  # noqa: F401 - fixtures/helpers shared
    FakeCapture,
    FakeEncoder,
    dev,
    fake_stream,
    wait_until,
)


@pytest.fixture
def env():
    fake_stream.atvs = {}
    captures = []
    alerts = []

    def capture_factory(sink, on_failure=None):
        cap = FakeCapture(sink, on_failure)
        captures.append(cap)
        return cap

    async def scan_fn(timeout=6):
        return [dev("Living Room"), dev("Living Room (2)")]

    engine = BridgeEngine(
        scan_fn=scan_fn,
        stream_fn=fake_stream,
        capture_factory=capture_factory,
        encoder_factory=lambda fmt: FakeEncoder(),
        on_alert=alerts.append,
    )
    try:
        yield engine, captures, alerts
    finally:
        engine.shutdown()


def start_streaming(engine):
    engine.rescan()
    engine.set_selected(["Living Room", "Living Room (2)"])
    engine.start_selected()
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)


def test_capture_death_restarts_session_with_fresh_capture(env):
    engine, captures, alerts = env
    start_streaming(engine)
    assert len(captures) == 1

    captures[0].on_failure()  # simulate WASAPI dying after sleep/resume

    assert wait_until(
        lambda: len(captures) == 2
        and engine.snapshot().state is EngineState.STREAMING
    )
    snap = engine.snapshot()
    assert captures[0].stopped and captures[1].started
    assert snap.capture_restarts == 1
    assert snap.selected == ("Living Room", "Living Room (2)")  # selection kept
    assert alerts == []  # a single recovery is silent


def test_rapid_capture_failures_stop_session_and_alert(env):
    engine, captures, alerts = env
    start_streaming(engine)

    for expected in range(1, 6):  # keep killing every capture that appears
        assert wait_until(lambda: len(captures) >= expected)
        captures[expected - 1].on_failure()
        if wait_until(
            lambda: engine.snapshot().state is EngineState.IDLE, timeout=0.5
        ):
            break

    assert wait_until(lambda: engine.snapshot().state is EngineState.IDLE)
    snap = engine.snapshot()
    assert snap.capture_restarts == 3  # 3 restarts allowed, then give up
    assert len(captures) == 4  # initial + 3 restarts
    assert all(c.stopped for c in captures)
    assert alerts and "capture" in alerts[0].lower()


def test_manual_reconnect_gets_fresh_restart_budget(env):
    """After 'capture keeps failing' gives up, the alert tells the user to
    Connect again - that retry must not inherit the spent budget and die on
    its first hiccup."""
    engine, captures, alerts = env
    start_streaming(engine)
    for expected in range(1, 6):
        assert wait_until(lambda: len(captures) >= expected)
        captures[expected - 1].on_failure()
        if wait_until(
            lambda: engine.snapshot().state is EngineState.IDLE, timeout=0.5
        ):
            break
    assert wait_until(lambda: engine.snapshot().state is EngineState.IDLE)
    baseline = len(captures)  # initial + 3 allowed restarts

    engine.start_selected()  # immediate manual reconnect, well inside 60s
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    assert len(captures) == baseline + 1

    captures[-1].on_failure()  # one hiccup must RESTART, not kill the session
    assert wait_until(lambda: len(captures) == baseline + 2)
    assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)
    assert len(alerts) == 1  # only the original give-up alert


def test_recovery_retries_a_device_that_is_still_settling(monkeypatch):
    """Real-world case from the logs: an HDMI audio endpoint dies (display
    sleep), and the immediate reopen fails with 'Insufficient memory' while
    Windows resets it. That must be retried, not left silent and idle."""
    from homepod_bridge import engine as engine_mod

    monkeypatch.setattr(engine_mod, "CAPTURE_REOPEN_DELAY", 0.01)
    fake_stream.atvs = {}  # these tests build their own engine, no env fixture
    captures = []
    alerts = []
    fails = {"left": 2}  # first two reopens fail, third succeeds

    def capture_factory(sink, on_failure=None):
        cap = FakeCapture(sink, on_failure)
        if captures and fails["left"] > 0:
            fails["left"] -= 1
            raise OSError(-9992, "Insufficient memory")
        captures.append(cap)
        return cap

    async def scan_fn(timeout=6):
        return [dev("Living Room")]

    engine = BridgeEngine(
        scan_fn=scan_fn,
        stream_fn=fake_stream,
        capture_factory=capture_factory,
        encoder_factory=lambda fmt: FakeEncoder(),
        on_alert=alerts.append,
    )
    try:
        engine.rescan()
        engine.set_selected(["Living Room"])
        engine.start_selected()
        assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)

        captures[0].on_failure()  # endpoint died
        assert wait_until(
            lambda: engine.snapshot().state is EngineState.STREAMING
            and len(captures) == 2,
            timeout=5.0,
        )
        assert fails["left"] == 0  # it really did retry through both failures
        assert alerts == []  # recovered on its own; no user-facing noise
    finally:
        engine.shutdown()


def test_recovery_gives_up_with_an_alert_when_the_device_never_returns(monkeypatch):
    from homepod_bridge import engine as engine_mod

    monkeypatch.setattr(engine_mod, "CAPTURE_REOPEN_DELAY", 0.01)
    fake_stream.atvs = {}  # these tests build their own engine, no env fixture
    monkeypatch.setattr(engine_mod, "CAPTURE_REOPEN_ATTEMPTS", 2)
    captures = []
    alerts = []

    def capture_factory(sink, on_failure=None):
        if captures:
            raise OSError(-9996, "Device unavailable")
        cap = FakeCapture(sink, on_failure)
        captures.append(cap)
        return cap

    async def scan_fn(timeout=6):
        return [dev("Living Room")]

    engine = BridgeEngine(
        scan_fn=scan_fn,
        stream_fn=fake_stream,
        capture_factory=capture_factory,
        encoder_factory=lambda fmt: FakeEncoder(),
        on_alert=alerts.append,
    )
    try:
        engine.rescan()
        engine.set_selected(["Living Room"])
        engine.start_selected()
        assert wait_until(lambda: engine.snapshot().state is EngineState.STREAMING)

        captures[0].on_failure()
        # State reaches IDLE after the FIRST failed attempt, so wait for the
        # give-up alert itself rather than racing the retry loop.
        assert wait_until(lambda: bool(alerts), timeout=5.0)
        assert "reopen" in alerts[0].lower()
        assert engine.snapshot().state is EngineState.IDLE
    finally:
        engine.shutdown()


def test_capture_failure_after_stop_is_ignored(env):
    engine, captures, alerts = env
    start_streaming(engine)
    engine.stop_streaming()
    captures[0].on_failure()  # stale callback from a dead thread
    time.sleep(0.2)
    assert engine.snapshot().state is EngineState.IDLE
    assert len(captures) == 1  # no phantom restart
    assert alerts == []
