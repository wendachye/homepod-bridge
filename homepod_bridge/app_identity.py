"""Windows app identity, so tray notifications say "HomePod Bridge".

Windows resolves a tray notification's header from the *process's*
AppUserModelID (AUMID), looked up against ``.lnk`` files in the Start Menu's
Programs tree. Verified on Windows 11 (build 26200) with pystray 0.19.5:

- No explicit AUMID: the shell synthesizes one and shows the **host exe's**
  file description - for ``pythonw.exe`` that string is literally "Python",
  which is what users saw.
- Explicit AUMID, no matching shortcut: the header shows the **raw ID
  string** and no icon at all - worse than the default.
- Explicit AUMID **plus** a Programs shortcut carrying it: header shows the
  shortcut's **file name**, icon shows the shortcut's icon.

So both halves below are required. A shortcut in Programs\\Startup does NOT
count - the app resolver ignores that subfolder - which is why the existing
Startup/Desktop shortcuts never helped.

Everything here is best-effort: identity is cosmetic and must never break
streaming, so failures are logged and swallowed.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import POINTER, byref, c_int, c_long, c_ulong, c_ushort, c_void_p, c_wchar_p
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "APP_AUMID",
    "APP_NAME",
    "ensure_windows_identity",
    "read_shortcut_aumid",
    "set_process_aumid",
    "start_menu_shortcut_path",
    "write_app_icon",
    "write_shortcut",
]

APP_NAME = "HomePod Bridge"  # the .lnk file name IS the notification header
APP_AUMID = "WendaChye.HomePodBridge"

_CLSCTX_INPROC_SERVER = 1
_VT_LPWSTR = 31
_STGM_READ = 0
_S_OK = 0


class _GUID(ctypes.Structure):
    _fields_ = [
        ("d1", c_ulong),
        ("d2", c_ushort),
        ("d3", c_ushort),
        ("d4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        ctypes.windll.ole32.CLSIDFromString(c_wchar_p(text), byref(self))


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", c_ulong)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", c_ushort),
        ("r1", c_ushort),
        ("r2", c_ushort),
        ("r3", c_ushort),
        ("p", c_void_p),
        ("p2", c_void_p),
    ]


def _com_call(ptr, index: int, restype, argtypes=(), *args):
    """Invoke method #index on the COM object's vtable."""
    vtbl = ctypes.cast(ptr, POINTER(c_void_p))[0]
    fn = ctypes.cast(vtbl, POINTER(c_void_p))[index]
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(fn)(ptr, *args)


def _release(ptr) -> None:
    if ptr:
        _com_call(ptr, 2, c_ulong)


def set_process_aumid(aumid: str = APP_AUMID) -> bool:
    """Give this process an explicit AUMID (must pair with a shortcut)."""
    if sys.platform != "win32":
        return False
    try:
        hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            c_wchar_p(aumid)
        )
        return hr == _S_OK
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("setting AppUserModelID failed", exc_info=True)
        return False


def start_menu_shortcut_path() -> Optional[Path]:
    """``Start Menu\\Programs\\HomePod Bridge.lnk`` for the current user."""
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        return None
    programs = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return programs / f"{APP_NAME}.lnk"


def write_app_icon(path: Path) -> Optional[Path]:
    """Render the tray artwork to a multi-size .ico (the toast icon)."""
    try:
        from .tray import STATE_COLORS, make_icon_image
        from .engine import EngineState

        path.parent.mkdir(parents=True, exist_ok=True)
        image = make_icon_image(STATE_COLORS[EngineState.STREAMING], size=256)
        image.save(path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
        return path
    except Exception:  # noqa: BLE001 - an icon-less shortcut still works
        logger.debug("app icon generation failed", exc_info=True)
        return None


def write_shortcut(
    lnk: Path,
    target: str,
    arguments: str,
    workdir: str,
    aumid: str = APP_AUMID,
    icon: Optional[str] = None,
) -> None:
    """Create a .lnk carrying ``System.AppUserModel.ID`` (raises on failure).

    WScript.Shell (used by shortcuts.py) cannot set property-store values,
    so this goes through IShellLinkW + IPropertyStore directly.
    """
    ole32 = ctypes.windll.ole32
    hr_init = ole32.CoInitialize(None)  # S_FALSE if COM is already up
    psl = c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            byref(_GUID("{00021401-0000-0000-C000-000000000046}")),  # ShellLink
            None,
            _CLSCTX_INPROC_SERVER,
            byref(_GUID("{000214F9-0000-0000-C000-000000000046}")),  # IShellLinkW
            byref(psl),
        )
        if hr != _S_OK or not psl:
            raise OSError(f"CoCreateInstance(ShellLink) failed: {hr:#010x}")

        # IShellLinkW vtable: 7 SetDescription, 9 SetWorkingDirectory,
        # 11 SetArguments, 17 SetIconLocation, 20 SetPath
        _com_call(psl, 20, c_long, (c_wchar_p,), c_wchar_p(target))
        _com_call(psl, 11, c_long, (c_wchar_p,), c_wchar_p(arguments))
        _com_call(psl, 9, c_long, (c_wchar_p,), c_wchar_p(workdir))
        _com_call(psl, 7, c_long, (c_wchar_p,), c_wchar_p("Stream this PC's audio to HomePods"))
        if icon:
            _com_call(psl, 17, c_long, (c_wchar_p, c_int), c_wchar_p(icon), 0)

        pps = c_void_p()
        hr = _com_call(
            psl, 0, c_long, (c_void_p, c_void_p),
            byref(_GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")),  # IPropertyStore
            byref(pps),
        )
        if hr != _S_OK or not pps:
            raise OSError(f"QueryInterface(IPropertyStore) failed: {hr:#010x}")
        try:
            value = _PROPVARIANT()
            value.vt = _VT_LPWSTR
            buf = ctypes.create_unicode_buffer(aumid)
            value.p = ctypes.cast(buf, c_void_p)
            key = _PROPERTYKEY(_GUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)
            # IPropertyStore vtable: 6 SetValue, 7 Commit
            hr = _com_call(pps, 6, c_long, (c_void_p, c_void_p), byref(key), byref(value))
            if hr != _S_OK:
                raise OSError(f"SetValue(AppUserModel.ID) failed: {hr:#010x}")
            hr = _com_call(pps, 7, c_long)
            if hr != _S_OK:
                raise OSError(f"IPropertyStore::Commit failed: {hr:#010x}")
        finally:
            _release(pps)

        ppf = c_void_p()
        hr = _com_call(
            psl, 0, c_long, (c_void_p, c_void_p),
            byref(_GUID("{0000010b-0000-0000-C000-000000000046}")),  # IPersistFile
            byref(ppf),
        )
        if hr != _S_OK or not ppf:
            raise OSError(f"QueryInterface(IPersistFile) failed: {hr:#010x}")
        try:
            lnk.parent.mkdir(parents=True, exist_ok=True)
            # IPersistFile vtable: 6 Save
            hr = _com_call(ppf, 6, c_long, (c_wchar_p, c_int), c_wchar_p(str(lnk)), 1)
            if hr != _S_OK:
                raise OSError(f"IPersistFile::Save failed: {hr:#010x}")
        finally:
            _release(ppf)
    finally:
        _release(psl)
        if hr_init in (0, 1):  # S_OK / S_FALSE must be balanced
            ole32.CoUninitialize()


def read_shortcut_aumid(lnk: Path) -> Optional[str]:
    """The AUMID stored on an existing .lnk, or None."""
    if not lnk.exists():
        return None
    ole32 = ctypes.windll.ole32
    hr_init = ole32.CoInitialize(None)
    psl = c_void_p()
    try:
        hr = ole32.CoCreateInstance(
            byref(_GUID("{00021401-0000-0000-C000-000000000046}")),
            None,
            _CLSCTX_INPROC_SERVER,
            byref(_GUID("{000214F9-0000-0000-C000-000000000046}")),
            byref(psl),
        )
        if hr != _S_OK or not psl:
            return None
        ppf = c_void_p()
        if _com_call(
            psl, 0, c_long, (c_void_p, c_void_p),
            byref(_GUID("{0000010b-0000-0000-C000-000000000046}")), byref(ppf),
        ) != _S_OK:
            return None
        try:
            # IPersistFile vtable: 5 Load
            if _com_call(
                ppf, 5, c_long, (c_wchar_p, c_ulong), c_wchar_p(str(lnk)), _STGM_READ
            ) != _S_OK:
                return None
        finally:
            _release(ppf)

        pps = c_void_p()
        if _com_call(
            psl, 0, c_long, (c_void_p, c_void_p),
            byref(_GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")), byref(pps),
        ) != _S_OK:
            return None
        try:
            key = _PROPERTYKEY(_GUID("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}"), 5)
            found = _PROPVARIANT()
            # IPropertyStore vtable: 5 GetValue
            if _com_call(
                pps, 5, c_long, (c_void_p, c_void_p), byref(key), byref(found)
            ) != _S_OK:
                return None
            if found.vt != _VT_LPWSTR:
                return None
            return ctypes.cast(found.p, c_wchar_p).value
        finally:
            _release(pps)
    except Exception:  # noqa: BLE001
        logger.debug("reading shortcut AUMID failed", exc_info=True)
        return None
    finally:
        _release(psl)
        if hr_init in (0, 1):
            ole32.CoUninitialize()


def _launch_command() -> tuple:
    """(target, arguments, workdir) that starts this app as installed."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent)
    from .shortcuts import TRAY_ARGUMENTS, resolve_pythonw

    root = Path(__file__).resolve().parent.parent
    return str(resolve_pythonw()), TRAY_ARGUMENTS, str(root)


def ensure_windows_identity() -> bool:
    """Set the AUMID and make sure a matching Start Menu shortcut exists.

    Idempotent and best-effort; returns True when notifications should show
    "HomePod Bridge" rather than "Python"."""
    if sys.platform != "win32":
        return False
    if not set_process_aumid():
        return False
    lnk = start_menu_shortcut_path()
    if lnk is None:
        return False
    try:
        if read_shortcut_aumid(lnk) == APP_AUMID:
            return True  # already registered
        from .config import default_config_path

        icon = write_app_icon(default_config_path().parent / "app.ico")
        target, arguments, workdir = _launch_command()
        write_shortcut(
            lnk, target, arguments, workdir, APP_AUMID, str(icon) if icon else None
        )
        logger.info("registered Start Menu identity at %s", lnk)
        return True
    except Exception:  # noqa: BLE001 - never break the tray over a toast name
        logger.debug("Start Menu identity registration failed", exc_info=True)
        return False
