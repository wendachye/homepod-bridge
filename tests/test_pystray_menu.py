"""Regression tests against *real* pystray menu construction.

The v0.3.0 bug: pystray validates action callables via
``__code__.co_argcount`` (default-valued params count!) and raises
``ValueError`` for anything but 0/1/2 parameters. These tests build the full
menu through pystray and simulate clicking every item, so any arity or
dispatch-wiring mistake fails in CI instead of on the user's tray.
"""
import os

import pytest
from pyatv.const import PairingRequirement

from homepod_bridge.airplay import DeviceInfo
from homepod_bridge.config import ConfigStore
from homepod_bridge.engine import EngineState, Snapshot


def _import_pystray():
    try:
        import pystray  # noqa: F401

        return pystray
    except Exception:
        os.environ["PYSTRAY_BACKEND"] = "dummy"  # headless CI fallback
        try:
            import pystray

            return pystray
        except Exception:
            pytest.skip("pystray backend unavailable in this environment")


def dev(name: str) -> DeviceInfo:
    return DeviceInfo(
        name=name,
        address="10.0.0.9",
        identifier=f"id-{name}",
        raop_pairing=PairingRequirement.NotNeeded,
    )


def snap(state, selected=(), connected=(), volume=50.0) -> Snapshot:
    return Snapshot(
        state=state,
        devices=(dev("Living Room"), dev("Living Room (2)")),
        selected=tuple("id-" + n for n in selected),
        connected=tuple("id-" + n for n in connected),
        volume=volume,
        volumes=tuple(("id-" + n, volume) for n in selected),
    )


class FakeEngine:
    """Records every call the tray dispatches; returns plausible snapshots."""

    def __init__(self, initial: Snapshot):
        self._snap = initial
        self.calls = []

    def _rec(self, name, *args):
        self.calls.append((name, *args))
        return self._snap

    def snapshot(self):
        return self._snap

    def rescan(self, timeout=6):
        return self._rec("rescan")

    def start_selected(self):
        return self._rec("start_selected")

    def stop_streaming(self):
        return self._rec("stop_streaming")

    def toggle_device(self, name):
        return self._rec("toggle_device", name)

    def set_selected(self, names):
        return self._rec("set_selected", tuple(names))

    def set_volume(self, level):
        return self._rec("set_volume", level)

    def set_volume_nowait(self, level):
        # Distinct key from the blocking variant: the tray must NEVER call
        # blocking set_volume from the flyout thread (20s freeze), and an
        # aliased recording would hide that regression.
        self._rec("set_volume_nowait", level)

    def set_device_volume(self, name, level):
        return self._rec("set_device_volume", name, level)

    def set_device_volume_nowait(self, name, level):
        self._rec("set_device_volume_nowait", name, level)

    def nudge_volume(self, delta):
        return self._rec("nudge_volume", delta)

    def shutdown(self):
        self.calls.append(("shutdown",))


def all_items(pystray, menu):
    for item in menu.items:
        yield item
        submenu = getattr(item, "submenu", None)
        if submenu is not None:
            yield from all_items(pystray, submenu)


def make_app(tmp_path, snapshot):
    import threading

    from homepod_bridge.tray import TrayApp

    engine = FakeEngine(snapshot)
    opened = {"event": threading.Event(), "kwargs": None}

    def fake_slider(**kwargs):
        opened["kwargs"] = kwargs
        opened["event"].set()

    app = TrayApp(
        engine=engine,
        store=ConfigStore(tmp_path / "config.json"),
        slider_factory=fake_slider,
    )
    return app, engine, opened


@pytest.mark.parametrize(
    "state,selected,connected",
    [
        (EngineState.IDLE, ("Living Room",), ()),
        (
            EngineState.STREAMING,
            ("Living Room", "Living Room (2)"),
            ("Living Room", "Living Room (2)"),
        ),
        (EngineState.CONNECTING, ("Living Room",), ()),
    ],
)
def test_menu_builds_and_every_item_is_clickable(
    tmp_path, monkeypatch, state, selected, connected
):
    pystray = _import_pystray()
    # "Open logs" dispatches for real: without these patches every run would
    # mkdir the real %APPDATA% and pop a File Explorer window per case.
    import homepod_bridge.logging_setup as logging_setup

    opened_paths = []
    monkeypatch.setattr(os, "startfile", opened_paths.append, raising=False)
    monkeypatch.setattr(logging_setup, "log_directory", lambda: tmp_path / "logs")

    s = snap(state, selected=selected, connected=connected)
    app, engine, _opened = make_app(tmp_path, s)

    menu = app._pystray_menu(s)  # v0.3.0 raised ValueError right here

    clicked = 0
    for item in all_items(pystray, menu):
        item(None)  # simulate the click; must never raise
        clicked += 1
    assert clicked > 6
    assert ("shutdown",) in engine.calls  # the Quit item really quits
    assert opened_paths == [tmp_path / "logs"]  # Open logs stayed sandboxed


def test_clicks_dispatch_to_engine_with_correct_arguments(tmp_path):
    pystray = _import_pystray()
    s = snap(EngineState.IDLE, selected=("Living Room",))
    app, engine, opened = make_app(tmp_path, s)
    menu = app._pystray_menu(s)
    items = {str(i): i for i in all_items(pystray, menu)}

    items["Connect"](None)  # idle -> toggle starts streaming
    items["Living Room (2)"](None)
    items["Rescan"](None)
    assert ("start_selected",) in engine.calls
    assert ("toggle_device", "id-Living Room (2)") in engine.calls
    from tests.test_engine import wait_until
    assert wait_until(lambda: ("rescan",) in engine.calls)

    items["Volume (50%)..."](None)  # opens the slider popup thread
    assert opened["event"].wait(timeout=2.0)
    assert opened["kwargs"]["initial"] == 50.0
    opened["kwargs"]["set_volume"](80.0)  # slider drag -> engine
    assert ("set_volume_nowait", 80.0) in engine.calls

    streaming = snap(
        EngineState.STREAMING,
        selected=("Living Room",),
        connected=("Living Room",),
        volume=50.0,
    )
    app._last = streaming
    engine._snap = streaming  # toggle is state-aware via engine.snapshot()
    vmenu = app._pystray_menu(streaming)
    vitems = {str(i): i for i in all_items(pystray, vmenu)}
    vitems["Disconnect"](None)  # streaming -> toggle stops
    assert ("stop_streaming",) in engine.calls

    checked_states = {
        str(i): i.checked for i in all_items(pystray, vmenu) if i.checked is not None
    }
    assert checked_states["Living Room"] is True
    assert checked_states["Living Room (2)"] is False


def test_volume_slider_gets_device_rows_when_multiple_selected(tmp_path):
    pystray = _import_pystray()
    s = snap(
        EngineState.STREAMING,
        selected=("Living Room", "Living Room (2)"),
        connected=("Living Room", "Living Room (2)"),
    )
    app, engine, opened = make_app(tmp_path, s)
    menu = app._pystray_menu(s)
    items = {str(i): i for i in all_items(pystray, menu)}

    items["Volume (50%)..."](None)
    assert opened["event"].wait(timeout=2.0)
    assert opened["kwargs"]["devices"] == [
        ("Living Room", 50.0),
        ("Living Room (2)", 50.0),
    ]
    opened["kwargs"]["set_device_volume"]("Living Room", 30.0)
    assert ("set_device_volume_nowait", "id-Living Room", 30.0) in engine.calls


def test_volume_slider_hides_device_rows_for_single_device(tmp_path):
    pystray = _import_pystray()
    s = snap(EngineState.STREAMING, selected=("Living Room",), connected=("Living Room",))
    app, engine, opened = make_app(tmp_path, s)
    menu = app._pystray_menu(s)
    items = {str(i): i for i in all_items(pystray, menu)}

    items["Volume (50%)..."](None)
    assert opened["event"].wait(timeout=2.0)
    assert opened["kwargs"]["devices"] == []  # classic single-slider UI


def test_single_device_slider_opens_at_effective_level(tmp_path):
    """One device selected with an override: the slider must start at the
    level the room is actually playing, not the master hiding behind it."""
    s = Snapshot(
        state=EngineState.STREAMING,
        devices=(dev("Living Room"),),
        selected=("Living Room",),
        connected=("Living Room",),
        volume=50.0,
        volumes=(("Living Room", 30.0),),
    )
    app, _engine, opened = make_app(tmp_path, s)
    app._open_volume_slider()
    assert opened["event"].wait(timeout=2.0)
    assert opened["kwargs"]["initial"] == 30.0
    assert opened["kwargs"]["devices"] == []  # still the single-slider UI


def test_slider_close_persists_master_and_device_volumes(tmp_path):
    """The replay on close must respect send chronology: a master drag
    clears overrides, later device tweaks layer on top."""
    s = snap(
        EngineState.STREAMING,
        selected=("Living Room", "Living Room (2)"),
        connected=("Living Room", "Living Room (2)"),
    )
    engine = FakeEngine(s)

    def scripted_slider(**kwargs):
        kwargs["set_volume"](80.0)  # master first: wipes any override...
        kwargs["set_device_volume"]("Living Room", 30.0)  # ...then a tweak

    store = ConfigStore(tmp_path / "config.json")
    from homepod_bridge.tray import TrayApp

    app = TrayApp(engine=engine, store=store, slider_factory=scripted_slider)
    app._last = s
    app._open_volume_slider()
    app._slider_thread.join(timeout=2.0)
    assert not app._slider_thread.is_alive()

    saved = store.load()
    assert saved.volume == 80.0
    assert saved.device_volumes == {"id-Living Room": 30.0}


def test_left_click_toggles_stream(tmp_path):
    """Single left-click fires Menu.__call__ on the default item (restored in
    0.6.0). It must dispatch the state-aware toggle: start when idle with a
    selection, stop when streaming."""
    pystray = _import_pystray()
    idle = snap(EngineState.IDLE, selected=("Living Room",))
    app, engine, _opened = make_app(tmp_path, idle)
    menu = pystray.Menu(app._menu_items)
    menu(None)  # what Icon.__call__ does on WM_LBUTTONUP
    assert ("start_selected",) in engine.calls

    streaming = snap(
        EngineState.STREAMING, selected=("Living Room",), connected=("Living Room",)
    )
    app._last = streaming
    engine._snap = streaming  # dispatch re-queries live state, not the label
    menu(None)
    assert ("stop_streaming",) in engine.calls


def test_left_click_is_inert_without_selection(tmp_path):
    """With nothing selected the toggle is disabled, and pystray would fire a
    disabled default item anyway - so there must be no default at all."""
    pystray = _import_pystray()
    s = snap(EngineState.IDLE)  # nothing selected
    app, engine, _opened = make_app(tmp_path, s)
    menu = pystray.Menu(app._menu_items)
    before = list(engine.calls)
    menu(None)
    assert engine.calls == before  # nothing dispatched


def test_lazy_menu_factory_tracks_current_state(tmp_path):
    pystray = _import_pystray()
    idle = snap(EngineState.IDLE, selected=("Living Room",))
    app, engine, _opened = make_app(tmp_path, idle)
    menu = pystray.Menu(app._menu_items)
    labels = {str(i) for i in all_items(pystray, menu)}
    assert "Connect" in labels and "Disconnect" not in labels

    app._last = snap(
        EngineState.STREAMING, selected=("Living Room",), connected=("Living Room",)
    )
    labels = {str(i) for i in all_items(pystray, menu)}  # same Menu object
    assert "Disconnect" in labels and "Connect" not in labels
