"""Release the volume UI service through its normal application shutdown path."""
import sys


def pytest_sessionfinish(session, exitstatus):
    module = sys.modules.get("homepod_bridge.volume_slider")
    if module is not None:
        module.shutdown_slider()
