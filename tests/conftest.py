"""Shared pytest configuration.

The flyout tests deliberately leave dormant Tk interpreters behind
(``volume_slider._RETIRED`` - see the 0.11.1 changelog): Tk must be torn
down by the thread that created it, and those short-lived worker threads
are gone. At interpreter shutdown CPython finalizes the survivors from the
main thread, and Tcl responds with ``Tcl_AsyncDelete: async handler deleted
by the wrong thread`` - aborting the process AFTER a fully green summary
(observed in CI: exit 1 on Windows 3.12, ``Aborted (core dumped)`` on
Linux; Python 3.14 happens to survive it). The tray app guards its own exit
the same way in ``run_tray``; this mirrors that for the test process.
"""
import os
import sys

import pytest

_exit_status = {"code": 0}


def pytest_sessionfinish(session, exitstatus):
    _exit_status["code"] = int(exitstatus)


@pytest.hookimpl(trylast=True)  # let other plugins (coverage etc.) finish first
def pytest_unconfigure(config):
    volume_slider = sys.modules.get("homepod_bridge.volume_slider")
    if volume_slider is not None and getattr(volume_slider, "_RETIRED", None):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(_exit_status["code"])
