import json

from pyatv.const import PairingRequirement

from homepod_bridge.airplay import DeviceInfo
from homepod_bridge.config import BridgeConfig, ConfigStore
from homepod_bridge.engine import EngineState, Snapshot
from homepod_bridge.tray import build_menu_spec, status_text


# ------------------------------------------------------------------ config
def test_config_roundtrip(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    cfg = BridgeConfig(
        devices=["Living Room", "Living Room (2)"],
        volume=72.0,
        device_volumes={"Living Room": 30.0},
        autoconnect=True,
    )
    store.save(cfg)
    loaded = store.load()
    assert loaded == cfg


def test_config_coerces_and_clamps_device_volumes(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"schema_version": 2, "device_volumes": {"Kitchen": "25", "Bedroom": 250}}), "utf-8"
    )
    loaded = ConfigStore(path).load()
    assert loaded.device_volumes == {"Kitchen": 25.0, "Bedroom": 100.0}


def test_config_wrong_typed_device_volumes_yield_defaults(tmp_path):
    path = tmp_path / "config.json"
    for payload in ('{"device_volumes": 5}', '{"device_volumes": {"a": null}}'):
        path.write_text(payload, "utf-8")
        assert ConfigStore(path).load() == BridgeConfig()


def test_config_nan_volume_yields_defaults_not_full_blast(tmp_path):
    """json.loads accepts the NaN literal, and a naive clamp turns it into
    100.0 - maximum volume from an undefined value."""
    path = tmp_path / "config.json"
    for payload in ('{"volume": NaN}', '{"device_volumes": {"K": NaN}}'):
        path.write_text(payload, "utf-8")
        assert ConfigStore(path).load() == BridgeConfig()


def test_config_missing_file_yields_defaults(tmp_path):
    loaded = ConfigStore(tmp_path / "nope.json").load()
    assert loaded == BridgeConfig()


def test_config_corrupt_file_yields_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json", "utf-8")
    assert ConfigStore(path).load() == BridgeConfig()


def test_config_clamps_volume_and_ignores_unknown_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"volume": 250, "future_key": 1}), "utf-8")
    loaded = ConfigStore(path).load()
    assert loaded.volume == 100.0
    assert loaded.devices == []


def test_config_wrong_types_yield_defaults_not_a_crash(tmp_path):
    """Valid JSON with wrong types used to escape the corrupt-config guard
    and crash the tray at every launch until the file was hand-deleted."""
    path = tmp_path / "config.json"
    for payload in ('{"volume": null}', '{"volume": "loud"}', '{"devices": 42}'):
        path.write_text(payload, "utf-8")
        assert ConfigStore(path).load() == BridgeConfig()


def test_config_coerces_field_types(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "volume": "72",
                "devices": [1, "Living Room"],
                "autoconnect": 1,
                "bitrate": "256",
                "quality": "3",
            }
        ),
        "utf-8",
    )
    cfg = ConfigStore(path).load()
    assert cfg.volume == 72.0
    assert cfg.devices == ["1", "Living Room"]
    assert cfg.autoconnect is True
    assert cfg.bitrate == 256 and cfg.quality == 3


# --------------------------------------------------------------- menu spec
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


def flat(items):
    for it in items:
        yield it
        yield from flat(it.children)


def by_label(items, label):
    return next(it for it in flat(items) if it.label == label)


def test_idle_menu_offers_connect_and_volume_slider():
    items = build_menu_spec(snap(EngineState.IDLE, selected=("Living Room",)), False)
    connect = by_label(items, "Connect")
    assert connect.action == "toggle_stream" and connect.enabled
    vol = by_label(items, "Volume (50%)...")
    assert vol.action == "volume_slider" and vol.enabled  # presets adjust even idle
    assert by_label(items, "Living Room").checked is True
    assert by_label(items, "Living Room (2)").checked is False
    assert by_label(items, "Rescan").action == "rescan"


def test_idle_menu_disables_connect_without_selection():
    items = build_menu_spec(snap(EngineState.IDLE), False)
    assert by_label(items, "Connect").enabled is False


def test_streaming_menu_offers_disconnect_and_volume():
    s = snap(
        EngineState.STREAMING,
        selected=("Living Room", "Living Room (2)"),
        connected=("Living Room", "Living Room (2)"),
        volume=75.0,
    )
    items = build_menu_spec(s, True)
    assert by_label(items, "Disconnect").action == "toggle_stream"
    assert status_text(s) == "Streaming to 2 of 2 device(s)"
    assert by_label(items, "Volume (75%)...").action == "volume_slider"
    assert by_label(items, "Auto-connect at launch").checked is True
    assert by_label(items, "Open logs").action == "open_logs"


def test_toggle_stream_is_the_only_default_item():
    """pystray fires the default item on a single left-click of the icon.
    Exactly the Connect/Disconnect toggle may be default (restored in 0.6.0
    now that all HMENU rebuilds are lock-serialized on icon-owned threads),
    and only while enabled - pystray fires a default item even if disabled."""
    for state in (EngineState.IDLE, EngineState.CONNECTING, EngineState.STREAMING):
        items = build_menu_spec(snap(state, selected=("Living Room",)), True)
        defaults = [it for it in flat(items) if it.default]
        assert [it.action for it in defaults] == ["toggle_stream"]
        assert all(it.enabled for it in defaults)


def test_no_default_item_while_toggle_is_disabled():
    """A disabled default would still fire on left-click (pystray never
    checks enabled on the activation path), so it must not be default."""
    items = build_menu_spec(snap(EngineState.IDLE), False)  # nothing selected
    assert all(not it.default for it in flat(items))


def test_volume_label_shows_single_device_effective_level():
    """With one device selected, an override (not the master) is what the
    user hears - the label must say so."""
    s = Snapshot(
        state=EngineState.STREAMING,
        devices=(dev("Living Room"),),
        selected=("Living Room",),
        connected=("Living Room",),
        volume=50.0,
        volumes=(("Living Room", 30.0),),
    )
    items = build_menu_spec(s, False)
    assert by_label(items, "Volume (30%)...").action == "volume_slider"


def test_audio_delay_submenu_marks_the_active_choice():
    """The receiver hold is the bulk of total latency, so it is worth a
    live control rather than a config-file edit plus restart."""
    from homepod_bridge.tray import LATENCY_CHOICES

    items = build_menu_spec(snap(EngineState.IDLE, selected=("Living Room",)),
                            False, 0.25)
    parent = by_label(items, "Audio delay (250 ms)")
    labels = [c.label for c in parent.children]
    assert len(labels) == len(LATENCY_CHOICES)
    assert labels[0].startswith("250 ms")
    checked = [c for c in parent.children if c.checked]
    assert len(checked) == 1 and checked[0].label.startswith("250 ms")
    assert checked[0].action == "latency:0.25"


def test_audio_delay_choice_persists_and_reconnects(tmp_path):
    from homepod_bridge import raop_latency
    from homepod_bridge.config import ConfigStore
    from homepod_bridge.tray import TrayApp

    raop_latency.restore()

    class Eng:
        def __init__(self):
            self.calls = []
            self._snap = snap(EngineState.STREAMING, selected=("Living Room",),
                              connected=("Living Room",))

        def snapshot(self):
            return self._snap

        def stop_streaming(self):
            self.calls.append("stop")
            return self._snap

        def start_selected(self):
            self.calls.append("start")
            return self._snap

        def shutdown(self):
            pass

    store = ConfigStore(tmp_path / "config.json")
    app = TrayApp(engine=Eng(), store=store, slider_factory=lambda **kw: None)
    app.dispatch("latency:0.25")

    assert store.load().raop_latency == 0.25  # persisted
    assert raop_latency.current() == 0.25  # applied to future sessions
    assert app.engine.calls == ["stop", "start"]  # reconnected to take effect
    raop_latency.restore()


def test_connecting_status_text():
    assert status_text(snap(EngineState.CONNECTING, selected=("x",))) == "Connecting..."


def test_legacy_config_migrates_unique_names_and_preserves_undiscovered_names(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.path.write_text(json.dumps({"devices": ["Living Room", "Kitchen"],
                                     "device_volumes": {"Living Room": 25, "Kitchen": 70}}))
    cfg = store.load()
    assert cfg.devices == [] and cfg.device_volumes == {}
    assert cfg.resolve_legacy([dev("Living Room")])
    assert cfg.devices == ["id-Living Room"]
    assert cfg.device_volumes == {"id-Living Room": 25}
    assert cfg.legacy_devices == ["Kitchen"]
    assert cfg.legacy_device_volumes == {"Kitchen": 70}
    store.save(cfg)
    assert store.load() == cfg


def test_ambiguous_legacy_selection_requires_explicit_reselection(tmp_path):
    from dataclasses import replace

    store = ConfigStore(tmp_path / "config.json")
    store.path.write_text(json.dumps({"devices": ["Bedroom"], "device_volumes": {"Bedroom": 95}}))
    cfg = store.load()
    devices = [replace(dev("Bedroom"), identifier="one"), replace(dev("Bedroom"), identifier="two")]
    assert cfg.resolve_legacy(devices)
    assert cfg.devices == [] and cfg.device_volumes == {}
    assert cfg.legacy_devices == [] and cfg.legacy_device_volumes == {}
    # An ambiguous saved name cannot later become attached to whichever
    # speaker happens to be online when the other disappears.
    assert not cfg.resolve_legacy(devices[:1])
    assert cfg.devices == []


def test_duplicate_names_have_distinct_labels_and_identifier_menu_actions():
    from dataclasses import replace

    devices = (replace(dev("Bedroom"), identifier="one"),
               replace(dev("Bedroom"), identifier="two"))
    s = Snapshot(EngineState.IDLE, devices, ("one",), (), 50)
    items = [item for item in flat(build_menu_spec(s, False)) if item.action and item.action.startswith("toggle:")]
    assert len({item.label for item in items}) == 2
    assert [item.action for item in items] == ["toggle:one", "toggle:two"]
    assert [item.checked for item in items] == [True, False]
    assert all(item.label.startswith("Bedroom") for item in items)


def test_duplicate_name_slider_row_routes_to_its_identifier(tmp_path):
    from dataclasses import replace
    from tests.test_pystray_menu import FakeEngine
    from homepod_bridge.tray import TrayApp

    devices = (replace(dev("Bedroom"), identifier="one"),
               replace(dev("Bedroom"), identifier="two"))
    s = Snapshot(EngineState.STREAMING, devices, ("one", "two"), ("one", "two"), 50,
                 volumes=(("one", 20), ("two", 70)))
    engine = FakeEngine(s)

    def slider(**kwargs):
        rows = kwargs["devices"]
        assert len({label for label, _ in rows}) == 2
        kwargs["set_device_volume"](rows[1][0], 35)

    store = ConfigStore(tmp_path / "config.json")
    app = TrayApp(engine=engine, store=store, slider_factory=slider)
    app._open_volume_slider()
    app._slider_thread.join(2)
    assert ("set_device_volume_nowait", "two", 35) in engine.calls
    assert store.load().device_volumes == {"two": 35}
