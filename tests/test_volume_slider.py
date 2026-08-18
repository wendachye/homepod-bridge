from homepod_bridge.volume_slider import (
    SendThrottle,
    flyout_position,
    value_to_x,
    x_to_value,
)


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
