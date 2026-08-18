import threading
import threading as _threading
import time


def test_resume_from_sleep_restarts_a_streaming_session(monkeypatch, tmp_path):
    """After sleep the AirPlay session is dead but nothing notices: RAOP
    audio is UDP so sending to a vanished HomePod never errors, and the
    capture thread keeps feeding silence, so pyatv's stream never ends.
    The tray must spot the resume itself and reconnect."""
    from homepod_bridge import tray as tray_mod
    from homepod_bridge.config import ConfigStore
    from homepod_bridge.engine import EngineState
    from homepod_bridge.tray import TrayApp

    class Eng:
        def __init__(self):
            self.calls = []
            self.state = EngineState.STREAMING

        def snapshot(self):
            from homepod_bridge.engine import Snapshot

            return Snapshot(state=self.state, devices=(), selected=("A",),
                            connected=("A",), volume=50.0)

        def stop_streaming(self):
            self.calls.append("stop")
            return self.snapshot()

        def start_selected(self):
            self.calls.append("start")
            return self.snapshot()

        def shutdown(self):
            pass

    monkeypatch.setattr(tray_mod, "RESUME_POLL_SECONDS", 0.01)
    monkeypatch.setattr(tray_mod, "RESUME_DRIFT_SECONDS", 0.5)

    # wall clock jumps an hour while monotonic barely moves = suspended
    jumped = {"n": 0}
    real_time = time.time

    def fake_time():
        jumped["n"] += 1
        return real_time() + (3600 if jumped["n"] > 2 else 0)

    monkeypatch.setattr(tray_mod.time, "time", fake_time)

    app = TrayApp(engine=Eng(), store=ConfigStore(tmp_path / "c.json"),
                  slider_factory=lambda **kw: None)
    t = _threading.Thread(target=app._watch_for_resume, daemon=True)
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "start" not in app.engine.calls:
        time.sleep(0.02)
    app._quitting.set()
    t.join(timeout=2)

    assert app.engine.calls[:2] == ["stop", "start"], (
        f"expected a reconnect after resume, got {app.engine.calls}"
    )

from pyatv.const import PairingRequirement

from homepod_bridge.airplay import DeviceInfo
from homepod_bridge.config import BridgeConfig, ConfigStore
from homepod_bridge.engine import EngineState, Snapshot
from homepod_bridge.tray import TrayApp


def dev(name: str) -> DeviceInfo:
    return DeviceInfo(
        name=name,
        address="10.0.0.9",
        identifier=f"id-{name}",
        raop_pairing=PairingRequirement.NotNeeded,
    )


class BootFakeEngine:
    """Simulates login: the first N scans see an empty network."""

    def __init__(self, empty_scans: int):
        self._remaining_empty = empty_scans
        self._devices = ()
        self._selected = ()
        self._state = EngineState.IDLE
        self.start_calls = 0
        self.started = threading.Event()

    def _snap(self) -> Snapshot:
        return Snapshot(
            state=self._state,
            devices=self._devices,
            selected=self._selected,
            connected=(),
            volume=50.0,
        )

    def snapshot(self):
        return self._snap()

    def rescan(self, timeout=6):
        if self._remaining_empty > 0:
            self._remaining_empty -= 1
            self._devices = ()
        else:
            self._devices = (dev("Living Room"), dev("Living Room (2)"))
        return self._snap()

    def set_selected(self, names):
        self._selected = tuple(names)
        return self._snap()

    def start_selected(self):
        self.start_calls += 1
        if {d.name for d in self._devices}.intersection(self._selected):
            self._state = EngineState.STREAMING
            self.started.set()
        return self._snap()

    def shutdown(self):
        pass


def make_app(tmp_path, engine) -> TrayApp:
    store = ConfigStore(tmp_path / "config.json")
    store.save(BridgeConfig(devices=["Living Room"], autoconnect=True))
    return TrayApp(engine=engine, store=store, slider_factory=lambda **kw: None)


def test_autoconnect_retries_until_network_is_up(tmp_path):
    engine = BootFakeEngine(empty_scans=3)
    app = make_app(tmp_path, engine)
    app._autoconnect_with_retry(attempts=10, delay=0.01)
    assert engine.started.wait(timeout=3.0)
    assert engine.snapshot().state is EngineState.STREAMING
    assert engine.start_calls == 1  # not called while the network was empty


def test_autoconnect_gives_up_after_max_attempts(tmp_path):
    engine = BootFakeEngine(empty_scans=999)
    app = make_app(tmp_path, engine)
    app._autoconnect_with_retry(attempts=4, delay=0.01)
    time.sleep(0.3)
    assert engine.start_calls == 0
    assert engine.snapshot().state is EngineState.IDLE


def test_autoconnect_stops_immediately_on_quit(tmp_path):
    engine = BootFakeEngine(empty_scans=999)
    app = make_app(tmp_path, engine)
    app._quitting.set()  # user hit Quit right after launch
    app._autoconnect_with_retry(attempts=50, delay=0.05)
    time.sleep(0.2)
    assert engine.start_calls == 0
