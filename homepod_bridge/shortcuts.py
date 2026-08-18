"""One-time creation of Start-at-login and Desktop shortcuts (Windows).

Runs a hidden PowerShell to create the ``.lnk`` files via ``WScript.Shell``,
resolving the Startup and Desktop folders through
``[Environment]::GetFolderPath`` so OneDrive-redirected Desktops are handled
correctly. The script text is built by a pure, unit-tested function.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Tuple

__all__ = [
    "APP_NAME",
    "ps_quote",
    "build_shortcut_script",
    "resolve_pythonw",
    "validate_target",
    "install",
]

APP_NAME = "HomePod Bridge"
TRAY_ARGUMENTS = "-m homepod_bridge tray"
CREATE_NO_WINDOW = 0x08000000


def ps_quote(value: str) -> str:
    """Quote for a single-quoted PowerShell string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def build_shortcut_script(
    target: str, arguments: str, workdir: str, app_name: str = APP_NAME
) -> str:
    t, a, w = ps_quote(target), ps_quote(arguments), ps_quote(workdir)
    n = ps_quote(app_name + ".lnk")
    return (
        "$ws = New-Object -ComObject WScript.Shell\n"
        "foreach ($folder in @([Environment]::GetFolderPath('Startup'), "
        "[Environment]::GetFolderPath('Desktop'))) {\n"
        f"  $sc = $ws.CreateShortcut((Join-Path $folder {n}))\n"
        f"  $sc.TargetPath = {t}\n"
        f"  $sc.Arguments = {a}\n"
        f"  $sc.WorkingDirectory = {w}\n"
        "  $sc.Save()\n"
        "}"
    )


def resolve_pythonw() -> Path:
    """Prefer the windowless interpreter next to the current one."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return pythonw if pythonw.exists() else exe


def validate_target(target: str, workdir: str) -> None:
    """Fail LOUDLY if the interpreter baked into the shortcuts cannot run the
    app. Double-clicking the installer runs it under the .pyw-associated
    Python, which may not be the one where the dependencies were installed -
    the shortcut would then die at every login with no window and no log."""
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    probe = subprocess.run(
        [target, "-c", "import homepod_bridge.cli"],
        cwd=workdir,
        capture_output=True,
        text=True,
        creationflags=flags,
    )
    if probe.returncode != 0:
        detail = (probe.stderr or "").strip().splitlines()
        raise RuntimeError(
            f"{target}\ncannot run HomePod Bridge (missing dependencies?).\n\n"
            "Re-run create_shortcuts.pyw with the Python where you installed "
            "the requirements, e.g.:\n"
            f'  <that python>\\pythonw.exe "{Path(workdir) / "create_shortcuts.pyw"}"\n\n'
            + ("\n".join(detail[-3:]) if detail else "")
        )


def install(project_root: Path) -> Tuple[str, str]:
    """Create both shortcuts; returns the (target, workdir) baked into them."""
    target = str(resolve_pythonw())
    workdir = str(Path(project_root).resolve())
    validate_target(target, workdir)
    script = build_shortcut_script(target, TRAY_ARGUMENTS, workdir)
    flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=True,
        creationflags=flags,
    )
    # Start Menu entry too: it is what makes Windows label notifications
    # "HomePod Bridge" (and it is launchable from Start Menu search).
    try:
        from .app_identity import ensure_windows_identity

        ensure_windows_identity()
    except Exception:  # noqa: BLE001 - cosmetic; shortcuts already created
        pass
    return target, workdir
