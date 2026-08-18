"""PyInstaller entry point for the tray app (HomePodBridge.exe)."""
from homepod_bridge.logging_setup import setup_logging
from homepod_bridge.tray import run_tray

setup_logging(filename="bridge-tray.log")

if __name__ == "__main__":
    raise SystemExit(run_tray())
