"""PyInstaller entry point for the tray app (HomePodBridge.exe)."""
import argparse
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description="HomePod Bridge tray application")
    parser.add_argument("--self-test", type=Path, metavar="REPORT.json",
                        help="check the packaged runtime and exit, writing a JSON report")
    args = parser.parse_args(argv)
    if args.self_test is not None:
        from homepod_bridge.smoke_check import run_smoke_check

        return run_smoke_check(args.self_test)

    from homepod_bridge.logging_setup import setup_logging
    from homepod_bridge.tray import run_tray

    setup_logging(filename="bridge-tray.log")
    return run_tray()

if __name__ == "__main__":
    raise SystemExit(main())
