import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from homepod_bridge import app_identity, shortcuts
from homepod_bridge.shortcuts import (
    APP_NAME,
    build_shortcut_script,
    install,
    ps_quote,
    resolve_pythonw,
    validate_target,
)


def test_ps_quote_escapes_single_quotes():
    assert ps_quote("plain") == "'plain'"
    assert ps_quote("wenda's PC") == "'wenda''s PC'"


def test_script_targets_startup_and_desktop_with_quoted_paths():
    script = build_shortcut_script(
        r"C:\Python314\pythonw.exe",
        "-m homepod_bridge tray",
        r"C:\Tools\homepod-bridge",
    )
    assert "[Environment]::GetFolderPath('Startup')" in script
    assert "[Environment]::GetFolderPath('Desktop')" in script
    assert r"'C:\Python314\pythonw.exe'" in script
    assert "'-m homepod_bridge tray'" in script
    assert r"'C:\Tools\homepod-bridge'" in script
    assert f"'{APP_NAME}.lnk'" in script
    assert "$sc.Save()" in script


def test_script_survives_apostrophes_in_paths():
    script = build_shortcut_script(
        "C:\\pythonw.exe", "-m x", "C:\\Users\\wenda's stuff"
    )
    assert "'C:\\Users\\wenda''s stuff'" in script


def test_resolve_pythonw_prefers_windowless_sibling(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(python))
    assert resolve_pythonw() == python  # no pythonw.exe yet -> fallback
    pythonw.write_bytes(b"")
    assert resolve_pythonw() == pythonw  # windowless sibling wins


def test_validate_target_accepts_working_interpreter(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(shortcuts.subprocess, "run", fake_run)
    validate_target(r"C:\py\pythonw.exe", r"C:\proj")  # must not raise
    assert calls[0][:3] == [r"C:\py\pythonw.exe", "-c", "import homepod_bridge.cli"]


def test_validate_target_rejects_broken_interpreter(monkeypatch):
    """Double-clicking the installer can run it under the .pyw-associated
    Python rather than the one holding the dependencies; that used to bake
    silently-broken Startup shortcuts."""

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1, stderr="ModuleNotFoundError: No module named 'pyatv'"
        )

    monkeypatch.setattr(shortcuts.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="pyatv"):
        validate_target(r"C:\sys\pythonw.exe", r"C:\proj")


def test_install_validates_before_creating_shortcuts(tmp_path, monkeypatch):
    calls = []
    identities = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(shortcuts.subprocess, "run", fake_run)
    # install() also refreshes the Start Menu identity through native COM,
    # independently of the mocked PowerShell call. Never touch the user's
    # real shortcut with the temporary interpreter used by this test.
    monkeypatch.setattr(
        app_identity, "ensure_windows_identity", lambda: identities.append(True)
    )
    (tmp_path / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))

    install(tmp_path)
    assert len(calls) == 2
    assert calls[0][1] == "-c"  # the interpreter probe runs first
    assert calls[1][0] == "powershell"  # then the shortcut script
    assert identities == [True]
