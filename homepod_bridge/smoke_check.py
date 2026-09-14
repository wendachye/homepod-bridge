"""Offline checks for an installed wheel or frozen Windows application."""
from __future__ import annotations

import importlib
import json
import sys
import traceback
from pathlib import Path


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

        if sys.platform == "win32":
            importlib.import_module("pyaudiowpatch")
            importlib.import_module("pystray._win32")
            from .volume_slider import open_slider, shutdown_slider

            try:
                for _ in range(2):
                    open_slider(50, lambda value: None, should_close=lambda: True)
            finally:
                shutdown_slider()
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
