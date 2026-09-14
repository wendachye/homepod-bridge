from homepod_bridge.volume_slider import (
    SendThrottle,
    flyout_position,
    value_to_x,
    x_to_value,
)

import threading
import weakref

import pytest

from homepod_bridge import volume_slider


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- throttle
def test_throttle_sends_first_value_then_gates_by_interval():
    clock = FakeClock()
    th = SendThrottle(interval=0.15, clock=clock)
    th.offer(30.0)
    assert th.poll() == 30.0  # first send is immediate
    th.offer(35.0)
    assert th.poll() is None  # too soon
    clock.t = 0.2
    assert th.poll() == 35.0  # interval elapsed


def test_throttle_keeps_only_latest_pending_value():
    clock = FakeClock()
    th = SendThrottle(interval=0.15, clock=clock)
    th.offer(10.0)
    assert th.poll() == 10.0
    for v in (20.0, 30.0, 40.0):  # rapid drag between sends
        th.offer(v)
    clock.t = 0.2
    assert th.poll() == 40.0  # intermediate values dropped
    assert th.poll() is None


def test_throttle_flush_bypasses_interval_for_final_value():
    clock = FakeClock()
    th = SendThrottle(interval=0.15, clock=clock)
    th.offer(10.0)
    assert th.poll() == 10.0
    th.offer(11.0)
    assert th.poll() is None
    assert th.flush() == 11.0  # release always lands
    assert th.flush() is None


def test_throttle_returning_to_last_sent_value_cancels_pending():
    # Drag 50 -> 60 -> 50 within one interval, then release: flushing the
    # stale 60 would set the device to 60 while the UI shows 50.
    th = SendThrottle(interval=0.15, clock=FakeClock(), initial=50.0)
    th.offer(60.0)
    th.offer(50.0)  # back where we started before anything was sent
    assert th.flush() is None


def test_throttle_reset_adopts_external_value():
    # The master slider moved this row: reset() must clear the pending send
    # and make the adopted value the new suppression baseline.
    th = SendThrottle(interval=0.0, clock=FakeClock(), initial=50.0)
    th.offer(60.0)
    th.reset(30.0)
    assert th.poll() is None  # pending 60 cancelled
    th.offer(30.0)
    assert th.poll() is None  # 30 already applied externally - suppressed
    th.offer(35.0)
    assert th.poll() == 35.0


def test_throttle_suppresses_duplicates_including_initial():
    th = SendThrottle(interval=0.0, clock=FakeClock(), initial=50.0)
    th.offer(50.0)  # tk fires the callback once on scale.set(initial)
    assert th.poll() is None
    th.offer(51.0)
    assert th.poll() == 51.0
    th.offer(51.0)
    assert th.poll() is None


# ------------------------------------------------------------ positioning
def test_flyout_sits_above_taskbar_at_right_edge():
    # 1920x1080 screen with a 48px taskbar -> work area bottom = 1032
    x, y = flyout_position(1920, 1032, 300, 64, margin=16)
    assert (x, y) == (1920 - 300 - 16, 1032 - 64 - 16)


def test_flyout_never_goes_negative_on_tiny_screens():
    x, y = flyout_position(200, 100, 300, 64, margin=16)
    assert x >= 16 and y >= 16


# --------------------------------------------------------- slider geometry
def test_x_value_mapping_roundtrip_and_clamping():
    left, right = 11, 289
    assert x_to_value(left, left, right) == 0.0
    assert x_to_value(right, left, right) == 100.0
    assert x_to_value(left - 50, left, right) == 0.0  # drag past the ends
    assert x_to_value(right + 50, left, right) == 100.0
    mid_x = value_to_x(50.0, left, right)
    assert abs(x_to_value(mid_x, left, right) - 50.0) <= 1.0
    assert value_to_x(0.0, left, right) == left
    assert value_to_x(100.0, left, right) == right


def test_x_to_value_degenerate_track_is_safe():
    assert x_to_value(5, 10, 10) == 0.0


def _logic_window():
    """Exercise real row/throttle routing without requiring a Tk display."""
    window = volume_slider.FlyoutWindow.__new__(volume_slider.FlyoutWindow)
    clock = FakeClock()
    events = []
    window._set_volume = lambda value: events.append(("master", value))
    window._set_device_volume = lambda name, value: events.append((name, value))
    window._seq = 0
    window._closing = False

    class Label:
        def config(self, **kwargs):
            self.text = kwargs["text"]

    def row(initial, emit):
        item = volume_slider._SliderRow.__new__(volume_slider._SliderRow)
        item.value = initial
        item.pct = Label()
        item.pct.text = str(int(initial))
        item.redraw = lambda: None
        item.throttle = SendThrottle(initial=initial, clock=clock)
        item._emit = emit
        item.touch_seq = 0
        return item

    window._master = row(50.0, window._send_master)
    window._device_rows = [
        row(30.0, lambda v: window._send_device("Kitchen", v)),
        row(70.0, lambda v: window._send_device("Bedroom", v)),
    ]
    return window, events, clock


def test_cancelled_master_preview_preserves_committed_room_override():
    window, events, _clock = _logic_window()
    kitchen = window._device_rows[0]
    window._apply_row(window._master, 60.0, True)
    window._apply_row(kitchen, 30.0, False)
    assert kitchen.value == 30.0
    window._apply_row(window._master, 65.0, True)
    window._apply_row(window._master, 60.0, False)
    window._master.flush()
    assert events == [("master", 60.0), ("Kitchen", 30.0)]
    assert window._master.value == 60.0
    assert kitchen.value == 30.0 and kitchen.pct.text == "30"
    assert window._device_rows[1].value == 60.0


def test_pending_master_updates_rooms_when_sent_before_new_room_command():
    window, events, _clock = _logic_window()
    kitchen, bedroom = window._device_rows
    window._apply_row(window._master, 60.0, True)
    window._apply_row(kitchen, 30.0, False)
    window._apply_row(window._master, 65.0, True)
    assert kitchen.value == 30.0
    window._apply_row(kitchen, 35.0, False)
    assert events == [
        ("master", 60.0), ("Kitchen", 30.0),
        ("master", 65.0), ("Kitchen", 35.0),
    ]
    assert (kitchen.value, bedroom.value) == (35.0, 65.0)


def test_slider_service_reuses_one_owner_and_releases_every_popup():
    owners, roots, windows, finalized = [], [], [], []

    class Root:
        def __init__(self):
            owners.append(threading.get_ident())
            roots.append(weakref.ref(self))

        def destroy(self):
            assert threading.get_ident() == owners[0]

        def __del__(self):
            finalized.append(threading.get_ident())

    class Window:
        def __init__(self, master, **kwargs):
            assert threading.get_ident() == owners[0]
            assert master is roots[0]()
            self.master = master
            windows.append(weakref.ref(self))

        def run(self):
            pass

        def close(self):
            assert threading.get_ident() == owners[0]

    service = volume_slider._SliderService(Root, Window)
    try:
        for _ in range(25):
            request = volume_slider._SliderRequest({})
            service.submit(request)
            assert request.done.wait(2)
            assert request.error is None
        assert len(roots) == 1
        assert all(ref() is None for ref in windows)
    finally:
        service.shutdown(2)
    assert roots[0]() is None
    assert finalized == owners
    assert owners[0] != threading.get_ident()


def test_shutdown_closes_active_popup_and_unblocks_queued_callers():
    entered = threading.Event()
    created = []

    class Root:
        def destroy(self):
            pass

    class Window:
        def __init__(self, master, should_close, **kwargs):
            self.should_close = should_close
            created.append(1)

        def run(self):
            entered.set()
            while not self.should_close():
                threading.Event().wait(0.005)

        def close(self):
            pass

    service = volume_slider._SliderService(Root, Window)
    first = volume_slider._SliderRequest({})
    second = volume_slider._SliderRequest({})
    service.submit(first)
    assert entered.wait(2)
    service.submit(second)
    service.shutdown(2)
    assert first.done.is_set() and second.done.is_set()
    assert created == [1]


def test_open_slider_blocks_until_its_own_popup_closes(monkeypatch):
    entered, close_popup, returned = (threading.Event() for _ in range(3))

    class Root:
        def destroy(self):
            pass

    class Window:
        def __init__(self, master, should_close, **kwargs):
            self.should_close = should_close

        def run(self):
            entered.set()
            while not self.should_close():
                threading.Event().wait(0.005)

        def close(self):
            pass

    service = volume_slider._SliderService(Root, Window)
    monkeypatch.setattr(volume_slider, "_service", service)

    def caller():
        volume_slider.open_slider(50, lambda v: None, should_close=close_popup.is_set)
        returned.set()

    thread = threading.Thread(target=caller)
    thread.start()
    try:
        assert entered.wait(2)
        assert not returned.is_set()
        close_popup.set()
        assert returned.wait(2)
    finally:
        volume_slider.shutdown_slider()
        thread.join(2)
    assert volume_slider._service is None


def test_cleanup_failure_releases_current_and_queued_callers():
    entered, release = threading.Event(), threading.Event()

    class Root:
        def destroy(self):
            pass

    class Window:
        def __init__(self, **kwargs):
            pass

        def run(self):
            entered.set()
            assert release.wait(2)

        def close(self):
            raise RuntimeError("teardown failed")

    service = volume_slider._SliderService(Root, Window)
    first = volume_slider._SliderRequest({})
    queued = volume_slider._SliderRequest({})
    service.submit(first)
    assert entered.wait(2)
    service.submit(queued)
    release.set()
    assert first.done.wait(2) and queued.done.wait(2)
    assert "teardown failed" in str(first.error)
    assert first.error.__traceback__ is None and first.error.__context__ is None
    assert queued.error is not None
    with pytest.raises(RuntimeError, match="shutting down"):
        service.submit(volume_slider._SliderRequest({}))
    service.shutdown(2)


def test_delayed_open_after_quit_does_not_start_an_interpreter(monkeypatch):
    monkeypatch.setattr(volume_slider, "_service", None)

    def forbidden_service():
        raise AssertionError("late popup created a Tk service after shutdown")

    monkeypatch.setattr(volume_slider, "_SliderService", forbidden_service)
    volume_slider.shutdown_slider()
    volume_slider.open_slider(50, lambda v: None, should_close=lambda: True)
    assert volume_slider._service is None


def test_root_teardown_failure_is_reported_without_cross_thread_traceback():
    owner, released, commands = [], [], []

    class Root:
        def __init__(self):
            owner.append(threading.get_ident())
            self._tclCommands = ["close-callback"]
            self.children = {}
            self.master = None
            self.tk = self

        def deletecommand(self, name):
            commands.append((name, threading.get_ident()))
            self.tk = None

        def destroy(self):
            raise RuntimeError("native window already destroyed")

        def __del__(self):
            released.append(threading.get_ident())

    class Window:
        def __init__(self, **kwargs):
            pass

        def run(self):
            pass

        def close(self):
            pass

    service = volume_slider._SliderService(Root, Window)
    request = volume_slider._SliderRequest({})
    service.submit(request)
    assert request.done.wait(2)
    with pytest.raises(RuntimeError, match="native window already destroyed"):
        service.shutdown(2)
    assert commands == [("close-callback", owner[0])]
    assert released == owner
