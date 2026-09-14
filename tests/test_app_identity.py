"""Windows notification identity.

Without this, Windows labels tray toasts with the HOST interpreter's file
description - literally "Python" for pythonw.exe. These tests never touch
the real Start Menu; they exercise the pieces against tmp_path.
"""
import sys

import pytest

from homepod_bridge.app_identity import (
    APP_AUMID,
    APP_NAME,
    read_shortcut_aumid,
    set_process_aumid,
    start_menu_shortcut_path,
    write_app_icon,
    write_shortcut,
)

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows shell identity APIs"
)


@windows_only
def test_shortcut_lands_in_programs_not_startup():
    """The app resolver ignores Programs\\Startup - a shortcut there gives a
    raw AUMID string as the header, so the path must be Programs itself."""
    lnk = start_menu_shortcut_path()
    assert lnk is not None
    assert lnk.name == f"{APP_NAME}.lnk"  # the file name IS the header text
    assert lnk.parent.name == "Programs"
    assert "Startup" not in lnk.parts


@windows_only
def test_shortcut_aumid_round_trips(tmp_path):
    lnk = tmp_path / f"{APP_NAME}.lnk"
    write_shortcut(
        lnk,
        target=sys.executable,
        arguments="-m homepod_bridge tray",
        workdir=str(tmp_path),
        aumid=APP_AUMID,
    )
    assert lnk.exists()
    assert read_shortcut_aumid(lnk) == APP_AUMID


@windows_only
def test_read_aumid_of_missing_or_plain_shortcut(tmp_path):
    assert read_shortcut_aumid(tmp_path / "nope.lnk") is None
    plain = tmp_path / "plain.lnk"
    write_shortcut(plain, sys.executable, "", str(tmp_path), aumid="")
    assert read_shortcut_aumid(plain) in (None, "")  # no usable identity


@windows_only
def test_set_process_aumid_succeeds():
    assert set_process_aumid("HomePodBridge.Test.Identity") is True


def test_app_icon_is_a_multi_size_ico(tmp_path):
    ico = write_app_icon(tmp_path / "app.ico")
    if ico is None:
        pytest.skip("Pillow ICO support unavailable")
    assert ico.exists() and ico.stat().st_size > 0
    from PIL import Image

    with Image.open(ico) as img:
        assert img.format == "ICO"
        sizes = img.info.get("sizes", set())
        assert (16, 16) in sizes and (256, 256) in sizes  # tray + toast sizes


def test_existing_identity_is_refreshed_after_installation_moves(tmp_path, monkeypatch):
    from homepod_bridge import app_identity, config

    lnk = tmp_path / "HomePod Bridge.lnk"
    launch = ["old-pythonw.exe", "-m homepod_bridge tray", "old-folder"]
    writes = []
    monkeypatch.setattr(app_identity.sys, "platform", "win32")
    monkeypatch.setattr(app_identity, "set_process_aumid", lambda: True)
    monkeypatch.setattr(app_identity, "start_menu_shortcut_path", lambda: lnk)
    monkeypatch.setattr(app_identity, "read_shortcut_aumid", lambda path: APP_AUMID)
    monkeypatch.setattr(app_identity, "write_app_icon", lambda path: None)
    monkeypatch.setattr(config, "default_config_path", lambda: tmp_path / "config.json")
    monkeypatch.setattr(app_identity, "_launch_command", lambda: tuple(launch))
    monkeypatch.setattr(app_identity, "write_shortcut", lambda *args: writes.append(args))
    assert app_identity.ensure_windows_identity()
    launch[:] = ["new-app.exe", "", "new-folder"]
    assert app_identity.ensure_windows_identity()
    assert writes[-1][1:4] == tuple(launch)
    assert len(writes) == 2
