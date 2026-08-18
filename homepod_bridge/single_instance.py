"""Single-instance guard - two tray apps means two capture taps and chaos.

Windows: a named mutex (released automatically when the process dies).
POSIX (dev/CI): an flock'd lockfile with the same semantics.
"""
from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["SingleInstance"]

_ERROR_ALREADY_EXISTS = 183


class SingleInstance:
    def __init__(self, name: str = "homepod-bridge") -> None:
        self._name = name
        self._handle = None
        self._fd = None

    def acquire(self) -> bool:
        """True if we are the only instance; keeps the lock until release()."""
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # Global\ so the guard holds across fast-user-switching sessions
            # (Local\ is per logon session: two logged-in users would each
            # run a capture tap fighting over the same HomePods).
            self._handle = kernel32.CreateMutexW(None, False, f"Global\\{self._name}")
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                # CreateMutexW returned a handle to the EXISTING mutex; keep
                # it open and the mutex would survive the owner's release,
                # wedging acquire-after-release forever.
                if self._handle:
                    kernel32.CloseHandle(self._handle)
                self._handle = None
                return False
            return bool(self._handle)

        import fcntl

        path = Path(tempfile.gettempdir()) / f"{self._name}.lock"
        self._fd = open(path, "w")  # noqa: SIM115 - held for process lifetime
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fd.close()
            self._fd = None
            return False

    def release(self) -> None:
        if sys.platform == "win32":
            if self._handle:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(self._handle)
                self._handle = None
            return
        if self._fd is not None:
            import fcntl

            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                self._fd.close()
                self._fd = None
