"""Offline checks for an installed wheel or frozen Windows application."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import traceback
from pathlib import Path


def _check_windows_ui() -> None:
    """Create and close two real popups through the persistent UI service."""
    from . import volume_slider

    original_window = volume_slider.FlyoutWindow
    created = 0

    class SmokeWindow(original_window):
        def __init__(self, *args, **kwargs):
            nonlocal created
            super().__init__(*args, **kwargs)
            created += 1
            # Close only after Tk has built the popup and entered its event loop.
            # A true should_close predicate would skip window creation entirely.
            self._schedule(0, self.close)

    volume_slider.FlyoutWindow = SmokeWindow
    try:
        for _ in range(2):
            volume_slider.open_slider(50, lambda value: None)
    finally:
        try:
            volume_slider.shutdown_slider()
        finally:
            volume_slider.FlyoutWindow = original_window
    if created != 2:
        raise RuntimeError(f"Tk reopen check created {created} windows; expected 2")


def run_smoke_check(report_path: Path) -> int:
    """Exercise packaged imports, codec and UI teardown without audio hardware."""
    checks = []
    report = {"ok": False, "checks": checks, "python": sys.version}
    try:
        for name in ("homepod_bridge.cli", "homepod_bridge.tray", "pyatv", "lameenc", "miniaudio", "PIL.Image"):
            importlib.import_module(name)
            checks.append(name)

        import miniaudio
        from .pcm_pipe import PcmPipe

        pipe = PcmPipe.live(48000, 2)
        pipe.feed_pcm(b"\x01\x00\x02\x00" * 1024)
        pipe.finish()
        with pipe.reader() as reader:
            decoded = miniaudio.decode(reader.read(), nchannels=2, sample_rate=48000)
        if len(decoded.samples) != 2048:
            raise RuntimeError("PCM decoder did not preserve the sample count")
        checks.append("PCM decode")

        from .native_sender import helper_path
        helper = helper_path()
        if getattr(sys, "frozen", False) and sys.platform == "win32" and helper is None:
            raise RuntimeError("frozen Windows build is missing HomePodSender.exe")
        if helper is not None:
            result = subprocess.run(
                [str(helper), "--self-test"], capture_output=True, text=True,
                timeout=10, check=True,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            status = json.loads(result.stdout)
            if not status.get("ok") or status.get("protocol") != 1:
                raise RuntimeError("native sender protocol self-test failed")
            checks.append("Native AirPlay 2 sender startup")

        if sys.platform == "win32":
            importlib.import_module("pyaudiowpatch")
            importlib.import_module("pystray._win32")
            _check_windows_ui()
            checks.extend(["Windows capture import", "Windows tray import", "Tk reopen and shutdown"])
        report["ok"] = True
    except Exception:
        report["error"] = traceback.format_exc()
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    raise SystemExit(run_smoke_check(parser.parse_args().report))
