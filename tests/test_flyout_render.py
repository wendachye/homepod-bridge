"""Rendered-layout regression tests for the volume flyout.

The v0.4.2 bug: at the fixed 300px geometry, the Canvas's default ~380px
size request squeezed the later-packed percentage label and close button to
zero width - invisible in real use, undetectable by pure-logic tests. These
tests build the *actual* tkinter window (skipped when no display is
available, e.g. headless CI without Xvfb) and assert every widget is visible
and interactive at the final geometry.
"""
import pytest

from homepod_bridge import volume_slider
from homepod_bridge.volume_slider import HEIGHT, WIDTH, FlyoutWindow


def make_flyout(initial=50.0, devices=(), sent_dev=None):
    tk = pytest.importorskip("tkinter")
    sent = []
    set_dev = None
    if sent_dev is not None:
        def set_dev(name, v):
            sent_dev.append((name, v))
    try:
        fw = FlyoutWindow(
            initial, sent.append, devices=devices, set_device_volume=set_dev
        )
    except tk.TclError:
        pytest.skip("no display available for tkinter")
    fw.root.update()
    return fw, sent


@pytest.fixture
def flyout():
    fw, sent = make_flyout()
    try:
        yield fw, sent
    finally:
        if not fw._closing:
            fw.close()


def test_percentage_is_visible_at_final_geometry(flyout):
    fw, _sent = flyout
    assert fw.root.winfo_width() == WIDTH
    assert fw.pct.winfo_ismapped(), "percentage label got squeezed out of the layout"
    assert fw.pct.winfo_width() > 0
    assert fw.pct.cget("text") == "50"
    # canvas fills the remainder, inside the window, leaving room for labels
    assert 0 < fw.canvas.winfo_width() < WIDTH - fw.pct.winfo_width()


def test_drag_to_end_updates_label_and_sends_final_value(flyout):
    fw, sent = flyout
    fw.root.update()
    right_edge = fw.canvas.winfo_width() - 1
    mid_y = fw.canvas.winfo_height() // 2
    fw.canvas.event_generate("<Button-1>", x=right_edge, y=mid_y)
    fw.canvas.event_generate("<ButtonRelease-1>", x=right_edge, y=mid_y)
    fw.root.update()
    assert fw.value == 100.0
    assert fw.pct.cget("text") == "100"
    assert sent and sent[-1] == 100.0  # release always flushes


def test_mouse_wheel_nudges_value(flyout):
    fw, _sent = flyout
    fw.root.event_generate("<MouseWheel>", delta=120)
    fw.root.update()
    assert fw.value == 52.0
    assert fw.pct.cget("text") == "52"


def test_close_flushes_pending_value(flyout):
    fw, sent = flyout
    fw._apply_row(fw._master, 30.0, True)  # first send goes out immediately
    fw._apply_row(fw._master, 31.0, True)  # within throttle window -> pending
    fw.close()
    assert sent[-1] == 31.0  # pending value delivered on close
    assert fw._closing


def test_windows_repeated_popups_share_one_interpreter_and_exit_normally():
    """Exercise native Tcl teardown in a subprocess, with no forced os._exit.

    Main-thread GC between worker-owned popups reproduced the original fatal
    Tcl_AsyncDelete crash. The owner must now release every popup immediately
    and finalize its single interpreter itself at shutdown.
    """
    import subprocess
    import sys
    import textwrap

    if sys.platform != "win32":
        pytest.skip("the production Tk worker is Windows-only")
    pytest.importorskip("tkinter")
    script = textwrap.dedent("""
        import gc
        import threading
        import weakref
        from homepod_bridge import volume_slider as slider

        roots, windows, owners, released_on = [], [], [], []
        opened = threading.Event()
        auto_close = True
        real_root = slider._new_root
        real_window = slider.FlyoutWindow

        def new_root():
            root = real_root()
            roots.append(weakref.ref(root,
                lambda ref: released_on.append(threading.get_ident())))
            owners.append(threading.get_ident())
            return root

        class Window(real_window):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                assert threading.get_ident() == owners[0]
                windows.append(weakref.ref(self))
                if auto_close:
                    self.root.after(30, self.close)
                opened.set()

        slider._new_root = new_root
        slider.FlyoutWindow = Window
        try:
            for _ in range(30):
                slider.open_slider(50, lambda value: None,
                    devices=[("Kitchen", 30), ("Bedroom", 70)])
                assert all(ref() is None for ref in windows)
                gc.collect()  # deliberately from the non-owning main thread
            assert len(roots) == 1
            assert owners[0] != threading.get_ident()

            # Shutdown must also close a visible popup and release its caller.
            auto_close = False
            opened.clear()
            caller = threading.Thread(target=slider.open_slider,
                args=(50, lambda value: None))
            caller.start()
            assert opened.wait(5)
            slider.shutdown_slider()
            caller.join(5)
            assert not caller.is_alive()
        finally:
            slider.shutdown_slider()
        gc.collect()
        assert roots[0]() is None
        assert released_on == owners
        assert slider._service is None
        print("normal shutdown completed")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "normal shutdown completed" in result.stdout


def test_send_failure_closes_window():
    tk = pytest.importorskip("tkinter")

    def broken(_v):
        raise RuntimeError("engine is gone")

    try:
        fw = FlyoutWindow(50.0, broken)
    except tk.TclError:
        pytest.skip("no display available for tkinter")
    fw.root.update()
    fw._apply_row(fw._master, 60.0, True)  # immediate send -> raises -> close()
    assert fw._closing


@pytest.mark.parametrize("count", [2, 3, 5])
def test_device_rows_fit_vertically(count):
    """The v0.4.2 squeeze bug, vertical axis: pack() silently clips the
    LAST-packed row when the window height under-budgets the content, and
    mapped/width checks alone can't see it."""
    devices = [(f"Room {i}", 40.0) for i in range(count)]
    fw, _sent = make_flyout(devices=devices, sent_dev=[])
    try:
        fw.root.update()
        win_h = fw.root.winfo_height()
        assert win_h > HEIGHT
        for row in fw._device_rows:
            assert row.canvas.winfo_height() >= row.canvas.winfo_reqheight()
            assert row.pct.winfo_height() >= row.pct.winfo_reqheight()
            bottom = (
                row.canvas.winfo_rooty()
                - fw.root.winfo_rooty()
                + row.canvas.winfo_height()
            )
            assert bottom <= win_h  # fully inside the window
    finally:
        if not fw._closing:
            fw.close()


def test_device_rows_render_and_send_per_device():
    sent_dev = []
    fw, sent = make_flyout(
        devices=[("Kitchen", 30.0), ("Bedroom", 70.0)], sent_dev=sent_dev
    )
    try:
        fw.root.update()
        assert fw.root.winfo_height() > HEIGHT
        assert len(fw._device_rows) == 2
        for row in fw._device_rows:
            assert row.canvas.winfo_ismapped() and row.canvas.winfo_width() > 0
            assert row.pct.winfo_ismapped() and row.pct.winfo_width() > 0
        kitchen, bedroom = fw._device_rows
        assert kitchen.pct.cget("text") == "30"
        assert bedroom.pct.cget("text") == "70"

        right = kitchen.canvas.winfo_width() - 1
        mid = kitchen.canvas.winfo_height() // 2
        kitchen.canvas.event_generate("<Button-1>", x=right, y=mid)
        kitchen.canvas.event_generate("<ButtonRelease-1>", x=right, y=mid)
        fw.root.update()
        assert sent_dev and sent_dev[-1] == ("Kitchen", 100.0)
        assert bedroom.value == 70.0  # the other room is untouched
        assert sent == []  # the master never fired
    finally:
        if not fw._closing:
            fw.close()


def test_master_drag_mirrors_device_rows_without_device_sends():
    sent_dev = []
    fw, sent = make_flyout(
        devices=[("Kitchen", 30.0), ("Bedroom", 70.0)], sent_dev=sent_dev
    )
    try:
        fw.root.update()
        right = fw.canvas.winfo_width() - 1
        mid = fw.canvas.winfo_height() // 2
        fw.canvas.event_generate("<Button-1>", x=right, y=mid)
        fw.canvas.event_generate("<ButtonRelease-1>", x=right, y=mid)
        fw.root.update()
        assert sent and sent[-1] == 100.0  # one master send...
        assert sent_dev == []  # ...no per-device sends (engine does set-all)
        for row in fw._device_rows:  # UI mirrors the engine's set-all
            assert row.value == 100.0 and row.pct.cget("text") == "100"
    finally:
        if not fw._closing:
            fw.close()


def test_all_slider_tracks_align_and_master_is_labeled():
    """Equal values must render thumbs at the same x across rows (all rows
    share one layout). And with rooms listed below, the master needs a name."""
    fw, _sent = make_flyout(
        devices=[("Kitchen", 62.0), ("Bedroom", 62.0)], sent_dev=[]
    )
    try:
        fw.root.update()
        assert fw.master_label is not None
        assert fw.master_label.cget("text") == "All devices"
        master_x = fw.canvas.winfo_rootx()
        master_w = fw.canvas.winfo_width()
        for row in fw._device_rows:
            assert row.canvas.winfo_rootx() == master_x
            assert row.canvas.winfo_width() == master_w
    finally:
        if not fw._closing:
            fw.close()


def test_master_only_flyout_has_no_master_label():
    fw, _sent = make_flyout()
    try:
        assert fw.master_label is None  # classic single-slider look unchanged
    finally:
        if not fw._closing:
            fw.close()


def test_master_click_at_current_value_leaves_device_rows_alone():
    """A click on the master thumb without moving it sends nothing
    (duplicate-suppressed) - so it must not mirror the rows either, or the
    UI would show levels the engine never applied."""
    sent_dev = []
    fw, sent = make_flyout(
        devices=[("Kitchen", 30.0), ("Bedroom", 70.0)], sent_dev=sent_dev
    )
    try:
        fw.root.update()
        fw._apply_row(fw._master, 50.0, True)  # master already displays 50
        assert sent == [] and sent_dev == []
        kitchen, bedroom = fw._device_rows
        assert kitchen.value == 30.0 and kitchen.pct.cget("text") == "30"
        assert bedroom.value == 70.0 and bedroom.pct.cget("text") == "70"
    finally:
        if not fw._closing:
            fw.close()


def test_device_touch_flushes_pending_master_first():
    """A stale pending master value must reach the engine BEFORE a newer
    device tweak - otherwise close() would flush the old master over it,
    wiping the user's last action."""
    sent_dev = []
    fw, sent = make_flyout(
        devices=[("Kitchen", 30.0), ("Bedroom", 70.0)], sent_dev=sent_dev
    )
    try:
        fw.root.update()
        fw._apply_row(fw._master, 52.0, True)  # first send goes immediately
        fw._apply_row(fw._master, 56.0, True)  # within interval -> pending
        fw._apply_row(fw._device_rows[0], 54.0, True)  # touch Kitchen
        assert sent == [52.0, 56.0]  # pending master delivered first
        assert sent_dev[-1] == ("Kitchen", 54.0)
        fw.close()
        assert sent == [52.0, 56.0]  # nothing stale left to flush
    finally:
        if not fw._closing:
            fw.close()


def test_cancelled_master_drag_keeps_room_labels_at_committed_values():
    sent_dev = []
    fw, sent = make_flyout(
        devices=[("Kitchen", 30.0), ("Bedroom", 70.0)], sent_dev=sent_dev
    )
    try:
        # Freeze the throttle clock so all touches remain within one interval.
        for row in fw._all_rows():
            row.throttle._clock = lambda: 0.0
        fw._apply_row(fw._master, 60.0, True)
        kitchen, bedroom = fw._device_rows
        fw._apply_row(kitchen, 30.0, False)
        fw._apply_row(fw._master, 65.0, True)
        fw._apply_row(fw._master, 60.0, False)
        assert sent == [60.0]
        assert sent_dev == [("Kitchen", 30.0)]
        assert kitchen.pct.cget("text") == "30"
        assert bedroom.pct.cget("text") == "60"
        assert fw.pct.cget("text") == "60"
    finally:
        fw.close()
