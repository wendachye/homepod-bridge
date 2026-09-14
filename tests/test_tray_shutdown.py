import threading

from homepod_bridge import volume_slider
from homepod_bridge.config import ConfigStore
from homepod_bridge.engine import EngineState
from homepod_bridge.tray import TrayApp
from tests.test_pystray_menu import FakeEngine, snap


def test_quit_flushes_and_saves_popup_before_engine_shutdown(tmp_path, monkeypatch):
    opened, release = threading.Event(), threading.Event()
    engine = FakeEngine(snap(EngineState.IDLE))
    store = ConfigStore(tmp_path / "config.json")

    def slider(**kwargs):
        opened.set()
        assert release.wait(2)
        kwargs["set_volume"](37)

    def shutdown_slider():
        assert ("shutdown",) not in engine.calls
        release.set()

    monkeypatch.setattr(volume_slider, "shutdown_slider", shutdown_slider)
    app = TrayApp(engine=engine, store=store, slider_factory=slider)
    app._open_volume_slider()
    assert opened.wait(1)
    app.quit()
    assert engine.calls.index(("set_volume_nowait", 37)) < engine.calls.index(("shutdown",))
    assert store.load().volume == 37
    assert not app._slider_thread.is_alive()
