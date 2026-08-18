"""Rotating file logging so windowless (pythonw) runs leave diagnostics.

Logs land in ``%APPDATA%/homepod-bridge/logs/bridge.log`` (rotated, 3
backups). A console handler is added only when a console exists - under
``pythonw`` ``sys.stderr`` is ``None``.
"""
from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .config import default_config_path

__all__ = ["log_directory", "logging_configured", "setup_logging"]

_MARK = "_homepod_bridge_handler"


def log_directory() -> Path:
    return default_config_path().parent / "logs"


def logging_configured() -> bool:
    """True if this app already installed its handlers on the root logger."""
    return any(getattr(h, _MARK, False) for h in logging.getLogger().handlers)


class _ResilientRotatingFileHandler(RotatingFileHandler):
    """Rotation that survives another process holding the log file open.

    On Windows, os.rename on an open file raises PermissionError; the stock
    handler then drops the record and retries (churning backups) on EVERY
    emit until the other process exits. Here a failed rollover keeps
    appending to the current file and backs off before retrying."""

    _RETRY_DELAY = 60.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._retry_at = 0.0

    def shouldRollover(self, record) -> bool:  # noqa: N802 - stdlib API
        if time.monotonic() < self._retry_at:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:  # noqa: N802 - stdlib API
        try:
            super().doRollover()
        except OSError:
            self._retry_at = time.monotonic() + self._RETRY_DELAY
            if self.stream is None:
                self.stream = self._open()


def setup_logging(
    verbose: bool = False,
    log_dir: Optional[Path] = None,
    max_bytes: int = 1_000_000,
    backup_count: int = 3,
    filename: str = "bridge.log",
) -> Path:
    """Idempotent: replaces this app's previous handlers on reconfiguration.

    ``filename`` lets each entry mode use its own file (bridge-tray.log vs
    bridge-cli.log) so a long-running tray and a debugging CLI never fight
    over one file's rotation."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        if getattr(handler, _MARK, False):
            root.removeHandler(handler)
            handler.close()

    directory = Path(log_dir) if log_dir else log_directory()
    directory.mkdir(parents=True, exist_ok=True)
    logfile = directory / filename
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = _ResilientRotatingFileHandler(
        logfile, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    setattr(file_handler, _MARK, True)
    root.addHandler(file_handler)

    if sys.stderr is not None:  # pythonw has no console
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        setattr(console, _MARK, True)
        root.addHandler(console)

    logging.getLogger(__name__).info("logging to %s", logfile)
    return logfile
