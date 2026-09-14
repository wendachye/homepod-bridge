import sys

import pytest

from homepod_bridge import volume_slider
from homepod_bridge.smoke_check import _check_windows_ui


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Tk lifecycle check")
def test_windows_smoke_creates_reopens_and_shuts_down_real_ui():
    original = volume_slider.FlyoutWindow
    _check_windows_ui()
    assert volume_slider._service is None
    assert volume_slider.FlyoutWindow is original


def test_ui_smoke_rejects_open_slider_that_skips_creating_windows(monkeypatch):
    original = volume_slider.FlyoutWindow
    stopped = []
    monkeypatch.setattr(volume_slider, "open_slider", lambda *args, **kwargs: None)
    monkeypatch.setattr(volume_slider, "shutdown_slider", lambda: stopped.append(True))

    with pytest.raises(RuntimeError, match="created 0 windows; expected 2"):
        _check_windows_ui()

    assert stopped == [True]
    assert volume_slider.FlyoutWindow is original


def test_ui_smoke_restores_window_factory_when_shutdown_fails(monkeypatch):
    original = volume_slider.FlyoutWindow
    monkeypatch.setattr(volume_slider, "open_slider", lambda *args, **kwargs: None)

    def fail_shutdown():
        raise RuntimeError("UI teardown failed")

    monkeypatch.setattr(volume_slider, "shutdown_slider", fail_shutdown)
    with pytest.raises(RuntimeError, match="UI teardown failed"):
        _check_windows_ui()

    assert volume_slider.FlyoutWindow is original
