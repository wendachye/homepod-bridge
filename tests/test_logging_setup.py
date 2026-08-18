import logging
import os
import sys

from homepod_bridge.logging_setup import logging_configured, setup_logging


def _our_handlers():
    return [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_homepod_bridge_handler", False)
    ]


def teardown_function(_fn):
    for h in _our_handlers():
        logging.getLogger().removeHandler(h)
        h.close()


def test_logs_land_in_file_and_rotate(tmp_path):
    logfile = setup_logging(log_dir=tmp_path, max_bytes=500, backup_count=2)
    log = logging.getLogger("homepod_bridge.test")
    for i in range(100):
        log.info("line %04d %s", i, "x" * 40)
    assert logfile.exists()
    assert "line" in logfile.read_text("utf-8")
    assert (tmp_path / "bridge.log.1").exists()  # rotation happened


def test_reconfiguration_is_idempotent(tmp_path):
    setup_logging(log_dir=tmp_path)
    setup_logging(log_dir=tmp_path)
    # exactly one file handler + at most one console handler, never stacking
    assert 1 <= len(_our_handlers()) <= 2


def test_survives_pythonw_null_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    logfile = setup_logging(log_dir=tmp_path)
    logging.getLogger("homepod_bridge.test").info("windowless")
    assert "windowless" in logfile.read_text("utf-8")
    assert len(_our_handlers()) == 1  # file handler only, no console


def test_per_mode_filename(tmp_path):
    logfile = setup_logging(log_dir=tmp_path, filename="bridge-tray.log")
    assert logfile.name == "bridge-tray.log"
    logging.getLogger("homepod_bridge.test").info("hello tray")
    assert "hello tray" in logfile.read_text("utf-8")


def test_logging_configured_reflects_state(tmp_path):
    assert logging_configured() is False
    setup_logging(log_dir=tmp_path)
    assert logging_configured() is True


def test_blocked_rotation_keeps_logging(tmp_path, monkeypatch):
    """Windows: another process holding the log open makes the rotation
    rename fail. The stock handler then drops EVERY record and churns the
    backups; ours must keep appending to the current file."""
    logfile = setup_logging(log_dir=tmp_path, max_bytes=300, backup_count=2)
    log = logging.getLogger("homepod_bridge.test")

    def deny(*_a, **_k):
        raise PermissionError("file in use by another process")

    monkeypatch.setattr(os, "rename", deny)
    monkeypatch.setattr(os, "replace", deny)
    for i in range(50):
        log.info("blocked rotation %04d %s", i, "x" * 40)  # must not raise

    text = logfile.read_text("utf-8")
    assert "blocked rotation 0049" in text  # records kept flowing
