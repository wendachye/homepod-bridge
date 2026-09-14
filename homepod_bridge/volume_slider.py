"""Volume flyout, styled and positioned like the Windows 11 volume flyout.

Tray context menus always dismiss when an item is clicked (OS behaviour), so
volume opens this flyout instead - anchored to the bottom-right of the work
area, just above the taskbar, the same place native tray flyouts appear.

Layout: a master slider on top (behaves like the classic single slider:
dragging it sets EVERY device), and - when two or more devices are streaming
- one labeled slider per device below it, so different rooms can run at
different levels. Each row is a :class:`_SliderRow`: custom-drawn canvas
(dark track, accent fill, round thumb) with its own :class:`SendThrottle`.
Mouse wheel adjusts the row under the pointer (master elsewhere). A dedicated
worker owns one Tk interpreter and all popup windows; callers exchange plain
requests and completion events, never tkinter objects.

Layout rule learned the hard way: the percentage label and close button are
packed on the *right first*, because pack() honours earlier widgets' size
requests first and a Canvas requests ~380px by default - in a fixed 300px
window it would squeeze later-packed widgets out of existence entirely.

Pure, unit-tested pieces: :class:`SendThrottle`, :func:`flyout_position`,
:func:`x_to_value` / :func:`value_to_x`. :class:`FlyoutWindow` is covered by
rendered-layout tests when a display is available.
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from typing import Callable, Optional, Sequence, Tuple

__all__ = [
    "SendThrottle",
    "flyout_position",
    "x_to_value",
    "value_to_x",
    "FlyoutWindow",
    "open_slider",
    "shutdown_slider",
]

WIDTH, HEIGHT = 300, 64
MARGIN = 16
SEND_INTERVAL = 0.15  # seconds between volume sends while dragging
WHEEL_STEP = 2.0

BG, BORDER = "#1f1f1f", "#3a3a3a"
FG, MUTED = "#eeeeee", "#8a8a8a"
TRACK, ACCENT = "#454545", "#4cc2ff"
THUMB_R = 7
TRACK_PAD = THUMB_R + 4  # keeps the thumb fully inside the canvas


class SendThrottle:
    """Rate-limits slider updates and suppresses duplicates.

    - ``offer(v)`` records the latest slider value; returning to the
      last-sent value cancels any queued pending update.
    - ``poll()`` returns a value to send only if the interval has elapsed.
    - ``flush()`` returns the pending value regardless of the interval
      (used on drag release / close so the final position always lands).
    - ``reset(v)`` adopts an externally-applied value (the master slider
      moved this row): nothing pending, and ``v`` becomes the baseline so
      re-offering it is suppressed.
    """

    def __init__(
        self,
        interval: float = SEND_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        initial: Optional[float] = None,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._pending: Optional[float] = None
        self._last_value: Optional[float] = initial
        self._last_time = float("-inf")

    def offer(self, value: float) -> None:
        # Returning to the last-sent value must CANCEL a queued pending
        # update, or a 50->60->50 drag within one interval flushes 60 while
        # the UI shows 50.
        self._pending = value if value != self._last_value else None

    def poll(self) -> Optional[float]:
        if self._pending is None:
            return None
        if self._clock() - self._last_time < self._interval:
            return None
        return self._take()

    def flush(self) -> Optional[float]:
        if self._pending is None:
            return None
        return self._take()

    def reset(self, value: float) -> None:
        self._pending = None
        self._last_value = value

    def _take(self) -> float:
        value, self._pending = self._pending, None
        self._last_value = value
        self._last_time = self._clock()
        return value


def flyout_position(
    work_right: int,
    work_bottom: int,
    width: int,
    height: int,
    margin: int = MARGIN,
) -> Tuple[int, int]:
    """Bottom-right corner of the work area - where native flyouts live."""
    x = max(margin, work_right - width - margin)
    y = max(margin, work_bottom - height - margin)
    return x, y


def x_to_value(x: float, left: float, right: float) -> float:
    if right <= left:
        return 0.0
    frac = (x - left) / (right - left)
    return float(round(min(100.0, max(0.0, frac * 100.0))))


def value_to_x(value: float, left: float, right: float) -> float:
    return left + (right - left) * (min(100.0, max(0.0, value)) / 100.0)


# ------------------------------------------------------- Windows integration
def _work_area(fallback_w: int, fallback_h: int) -> Tuple[int, int]:
    """(right, bottom) of the desktop work area - excludes the taskbar."""
    if sys.platform == "win32":
        try:
            import ctypes

            rect = (ctypes.c_long * 4)()
            SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, rect, 0):
                return int(rect[2]), int(rect[3])
        except Exception:  # noqa: BLE001 - cosmetic; fall back below
            pass
    return fallback_w, fallback_h - 48


def _round_corners(root) -> None:
    """Ask DWM for Win11 rounded corners (no-op elsewhere / on failure)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND = 33, 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref)
        )
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


# ------------------------------------------------------------------- rows
class _SliderRow:
    """One custom-drawn slider (canvas + % label) with its own throttle.

    Rows never talk to the engine directly: ``on_apply`` routes through the
    window (which sequences master-vs-device interactions), and ``emit`` is
    the window-wrapped send for this row.
    """

    def __init__(self, tk, parent, initial: float, emit, on_apply) -> None:
        self.value = float(round(min(100.0, max(0.0, float(initial)))))
        self._emit = emit
        self._on_apply = on_apply
        self.throttle = SendThrottle(initial=self.value)
        self.touch_seq = 0  # chronological ordering for flush on close
        self.pct = tk.Label(
            parent,
            text=f"{int(self.value)}",
            fg=FG,
            bg=BG,
            font=("Segoe UI", 13),
            width=3,
            anchor="e",
        )
        self.pct.pack(side="right", padx=(10, 8))
        self.canvas = tk.Canvas(
            parent,
            width=10,  # minimal request; expand=True grows it to fit
            height=THUMB_R * 2 + 10,
            bg=BG,
            highlightthickness=0,
            cursor="hand2",
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_drag)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Configure>", self.redraw)

    def _track_bounds(self) -> Tuple[float, float]:
        w = self.canvas.winfo_width()
        return TRACK_PAD, max(TRACK_PAD + 1, w - TRACK_PAD)

    def redraw(self, *_args) -> None:
        c = self.canvas
        c.delete("all")
        left, right = self._track_bounds()
        cy = c.winfo_height() / 2
        c.create_line(left, cy, right, cy, fill=TRACK, width=4, capstyle="round")
        tx = value_to_x(self.value, left, right)
        c.create_line(left, cy, tx, cy, fill=ACCENT, width=4, capstyle="round")
        c.create_oval(
            tx - THUMB_R, cy - THUMB_R, tx + THUMB_R, cy + THUMB_R,
            fill=ACCENT, outline=BG, width=2,
        )

    def set_display(self, v: float) -> None:
        """Adopt an externally-applied value (the master moved this row):
        update visuals and throttle baseline without sending anything."""
        v = float(round(min(100.0, max(0.0, v))))
        self.value = v
        self.pct.config(text=f"{int(v)}")
        self.redraw()
        self.throttle.reset(v)

    def apply(self, v: float, live: bool, seq: int) -> None:
        v = float(round(min(100.0, max(0.0, v))))
        self.touch_seq = seq
        if v != self.value:
            self.value = v
            self.pct.config(text=f"{int(v)}")
            self.redraw()
        self.throttle.offer(v)
        due = self.throttle.poll() if live else self.throttle.flush()
        if due is not None:
            self._emit(due)

    def poll(self) -> None:
        due = self.throttle.poll()
        if due is not None:
            self._emit(due)

    def flush(self) -> None:
        due = self.throttle.flush()
        if due is not None:
            self._emit(due)

    def _on_drag(self, event) -> None:
        left, right = self._track_bounds()
        self._on_apply(self, x_to_value(event.x, left, right), True)

    def _on_release(self, event) -> None:
        left, right = self._track_bounds()
        self._on_apply(self, x_to_value(event.x, left, right), False)


# ------------------------------------------------------------------- window
class FlyoutWindow:
    """The flyout itself. Construct, then :meth:`run` to block until closed."""

    def __init__(
        self,
        initial: float,
        set_volume: Callable[[float], object],
        title: str = "Volume",  # kept for API compatibility; flyout is chromeless
        should_close: Optional[Callable[[], bool]] = None,
        devices: Sequence[Tuple[str, float]] = (),
        set_device_volume: Optional[Callable[[str, float], object]] = None,
        *,
        master=None,
    ) -> None:
        import tkinter as tk

        self._set_volume = set_volume
        self._set_device_volume = set_device_volume
        self._should_close = should_close
        self._closing = False
        self._seq = 0  # global touch counter across rows
        self._after_ids: set = set()
        self._master = None
        self._device_rows: list = []
        self._row_widgets: dict = {}
        self.canvas = self.pct = self.master_label = None
        self.root = None
        try:
            self._build(tk, initial, devices, master)
        except Exception:
            self.close()
            raise

    def _build(self, tk, initial, devices, master) -> None:
        self.root = tk.Tk() if master is None else tk.Toplevel(master)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BORDER)  # outer 1px border

        inner = tk.Frame(self.root, bg=BG)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.master_label = None
        if devices:
            # With per-room rows below, an unlabeled top slider is ambiguous.
            self.master_label = tk.Label(
                inner, text="All devices", fg=MUTED, bg=BG,
                font=("Segoe UI", 9), anchor="w",
            )
            self.master_label.pack(fill="x", padx=16, pady=(8, 0))
            row = tk.Frame(inner, bg=BG)
            row.pack(fill="x", padx=14, pady=(0, 8))
        else:
            row = tk.Frame(inner, bg=BG)
            row.pack(fill="both", expand=True, padx=14, pady=10)

        # The row builds right-side widgets FIRST (inside _SliderRow) so
        # pack() reserves the % label's space before the canvas, whose
        # default size request exceeds the whole window. No close button:
        # Esc and click-away close the flyout, and identical row layouts
        # keep every track aligned (equal values = same thumb position).
        self._master = _SliderRow(tk, row, initial, self._send_master, self._apply_row)

        self._device_rows: list = []
        self._row_widgets: dict = {
            self._master.canvas: self._master,
            self._master.pct: self._master,
        }
        if self.master_label is not None:
            self._row_widgets[self.master_label] = self._master
        for name, level in devices:
            label = tk.Label(
                inner, text=str(name), fg=MUTED, bg=BG,
                font=("Segoe UI", 9), anchor="w",
            )
            label.pack(fill="x", padx=16)
            dframe = tk.Frame(inner, bg=BG)
            dframe.pack(fill="x", padx=14, pady=(0, 8))
            drow = _SliderRow(
                tk,
                dframe,
                level,
                (lambda v, n=str(name): self._send_device(n, v)),
                self._apply_row,
            )
            drow.name = str(name)
            self._device_rows.append(drow)
            for w in (label, dframe, drow.canvas, drow.pct):
                self._row_widgets[w] = drow

        # Aliases: the master row IS the classic single slider (render tests
        # and callers use fw.canvas / fw.pct / fw.value).
        self.pct = self._master.pct
        self.canvas = self._master.canvas

        self.root.bind("<MouseWheel>", self._on_wheel)
        self.root.bind("<Escape>", self.close)
        self.root.bind("<FocusOut>", self._on_focus_out)

        self.root.update_idletasks()
        # Measure the real content height: fonts/DPI vary, and a fixed
        # per-row constant clips the LAST-packed row (the v0.4.2 squeeze
        # bug, vertical axis). +2 covers the 1px outer border padding.
        height = HEIGHT if not self._device_rows else max(
            HEIGHT, inner.winfo_reqheight() + 2
        )
        work_right, work_bottom = _work_area(
            self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        )
        x, y = flyout_position(work_right, work_bottom, WIDTH, height)
        self.root.geometry(f"{WIDTH}x{height}+{x}+{y}")
        _round_corners(self.root)
        self.root.deiconify()
        self.root.focus_force()
        self._schedule(int(SEND_INTERVAL * 1000), self._pump)

    # ------------------------------------------------------------ behaviour
    @property
    def value(self) -> float:
        return self._master.value

    def _all_rows(self):
        if self._master is None:
            return []
        return [self._master, *self._device_rows]

    def _apply_row(self, row, v: float, live: bool) -> None:
        self._seq += 1
        if row is not self._master:
            # A device touch must deliver any PENDING master value first, so
            # engine arrival order matches interaction order - otherwise
            # close() could flush a stale master over this newer tweak.
            self._master.flush()
        row.apply(v, live, self._seq)

    def _send_master(self, v: float) -> None:
        try:
            self._set_volume(v)
        except RuntimeError:
            self.close()  # engine loop closed (quit) - nothing left to talk to
        except Exception:  # noqa: BLE001 - transient failure; keep the flyout
            pass
        else:
            # Only an emitted command changes the devices. A pending master
            # drag can return to its baseline and be cancelled by the throttle;
            # mirroring that preview would erase room overrides only in the UI.
            for row in self._device_rows:
                row.set_display(v)

    def _send_device(self, name: str, v: float) -> None:
        if self._set_device_volume is None:
            return
        try:
            self._set_device_volume(name, v)
        except RuntimeError:
            self.close()
        except Exception:  # noqa: BLE001 - transient failure; keep the flyout
            pass

    def _row_under_pointer(self, event):
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:  # noqa: BLE001
            return None
        for _ in range(4):  # climb a few levels: canvas/label -> frame -> inner
            if w is None:
                return None
            row = self._row_widgets.get(w)
            if row is not None:
                return row
            w = getattr(w, "master", None)
        return None

    def _on_wheel(self, event) -> None:
        row = self._row_under_pointer(event) or self._master
        step = WHEEL_STEP if event.delta > 0 else -WHEEL_STEP
        self._apply_row(row, row.value + step, True)

    def _pump(self) -> None:
        if self._closing:
            return
        if self._should_close is not None and self._should_close():
            self.close()  # the tray is quitting; tear down our Tk on THIS thread
            return
        for r in self._all_rows():
            r.poll()
            if self._closing:
                return
        self._schedule(int(SEND_INTERVAL * 1000), self._pump)

    def _schedule(self, delay: int, callback) -> None:
        if self._closing:
            return

        def run_callback():
            self._after_ids.discard(token)
            callback()

        token = self.root.after(delay, run_callback)
        self._after_ids.add(token)

    def _on_focus_out(self, _event) -> None:
        if self._closing:
            return
        # Delay so focus moving between our own widgets doesn't close us.
        self._schedule(
            80,
            lambda: None
            if self._closing or self.root.focus_displayof()
            else self.close(),
        )

    def close(self, *_args) -> None:
        if self._closing:
            return
        self._closing = True
        # Flush in the order the user last touched the rows, so an older
        # master drag cannot wipe a newer per-device tweak (and vice versa).
        for row in sorted(self._all_rows(), key=lambda r: r.touch_seq):
            try:
                row.flush()
            except Exception:  # noqa: BLE001
                pass
        for token in self._after_ids:
            try:
                self.root.after_cancel(token)
            except Exception:  # noqa: BLE001
                pass
        self._after_ids.clear()
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001 - window already gone
            pass
        finally:
            # Break Python callback/widget cycles while still on the Tk owner.
            # Destroying widgets alone does not release their interpreter if
            # rows keep bound methods pointing back to this window.
            for row in self._all_rows():
                row._emit = None
                row._on_apply = None
            self._row_widgets.clear()
            self._device_rows.clear()
            self._master = None
            self.canvas = self.pct = self.master_label = None
            self.root = None

    def run(self) -> None:
        if not self._closing:
            try:
                self.root.wait_window()
            finally:
                self.close()


class _SliderRequest:
    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs
        self.done = threading.Event()
        self.error: Optional[RuntimeError] = None


def _new_root():
    import tkinter as tk

    root = tk.Tk()
    # Widgets always receive an explicit parent. Do not leave this worker's
    # interpreter reachable through tkinter's process-wide default root.
    if getattr(tk, "_default_root", None) is root:
        tk._default_root = None
    try:
        root.withdraw()
    except Exception:
        _destroy_root(root)
        raise
    return root


def _destroy_root(root) -> None:
    """Release Python/Tcl callback cycles even if native window teardown fails."""
    widgets, pending = [], [root]
    while pending:
        widget = pending.pop()
        widgets.append(widget)
        pending.extend(getattr(widget, "children", {}).values())
    try:
        root.destroy()
    finally:
        for widget in widgets:
            for command in getattr(widget, "_tclCommands", None) or ():
                try:
                    widget.tk.deletecommand(command)
                except Exception:  # noqa: BLE001 - command may already be gone
                    pass
            if hasattr(widget, "_tclCommands"):
                widget._tclCommands = None
            if hasattr(widget, "children"):
                widget.children.clear()
            if hasattr(widget, "master"):
                widget.master = None


class _SliderService:
    """Own the interpreter for the entire application, including finalization."""

    def __init__(self, root_factory=None, window_factory=None) -> None:
        self._root_factory = root_factory or _new_root
        self._window_factory = window_factory or FlyoutWindow
        self._requests: queue.Queue = queue.Queue()
        self._stopping = threading.Event()
        self._teardown_error: Optional[str] = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, name="volume-ui", daemon=True
        )
        self._thread.start()

    def submit(self, request: _SliderRequest) -> None:
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("Volume flyout is shutting down")
            self._requests.put(request)

    def shutdown(self, timeout: float) -> None:
        with self._lock:
            self._stopping.set()
            self._requests.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError("Volume flyout did not finish shutting down")
        if self._teardown_error is not None:
            raise RuntimeError(self._teardown_error)

    def _run(self) -> None:
        # These references remain local to the owner thread. No service field,
        # result, or propagated exception may retain a Tk widget/interpreter.
        root = None
        try:
            while True:
                request = self._requests.get()
                if request is None:
                    return
                window = None
                cleanup_failed = False
                try:
                    if self._stopping.is_set():
                        continue
                    should_close = request.kwargs.get("should_close")
                    if should_close is not None and should_close():
                        continue
                    if root is None:
                        root = self._root_factory()
                    kwargs = dict(request.kwargs)
                    kwargs["should_close"] = lambda: self._stopping.is_set() or (
                        should_close is not None and should_close()
                    )
                    window = self._window_factory(master=root, **kwargs)
                    window.run()
                except Exception as exc:  # noqa: BLE001 - report on calling thread
                    # The original traceback can hold widgets; never send it
                    # across threads or let the caller retain the interpreter.
                    request.error = RuntimeError(f"Could not open volume flyout: {exc}")
                finally:
                    try:
                        if window is not None:
                            window.close()
                    except Exception as exc:  # noqa: BLE001
                        request.error = RuntimeError(f"Could not close volume flyout: {exc}")
                        cleanup_failed = True
                    finally:
                        window = None
                        request.done.set()
                        request = None
                if cleanup_failed:
                    return  # do not reuse an interpreter after incomplete cleanup
        finally:
            with self._lock:
                self._stopping.set()
                while True:
                    try:
                        pending = self._requests.get_nowait()
                    except queue.Empty:
                        break
                    if pending is not None:
                        pending.error = RuntimeError("Volume flyout has stopped")
                        pending.done.set()
            try:
                if root is not None:
                    _destroy_root(root)
            except Exception as exc:  # noqa: BLE001 - no Tk traceback crosses threads
                self._teardown_error = f"Could not shut down volume UI: {exc}"
            finally:
                root = None  # release the interpreter on its creating thread


_service: Optional[_SliderService] = None
_service_lock = threading.Lock()


def open_slider(
    initial: float,
    set_volume: Callable[[float], object],
    title: str = "Volume",
    should_close: Optional[Callable[[], bool]] = None,
    devices: Sequence[Tuple[str, float]] = (),
    set_device_volume: Optional[Callable[[str, float], object]] = None,
) -> None:
    """Block until this popup closes; all Tk work runs on one persistent owner."""
    global _service
    request = _SliderRequest(
        dict(
            initial=initial,
            set_volume=set_volume,
            title=title,
            should_close=should_close,
            devices=devices,
            set_device_volume=set_device_volume,
        )
    )
    with _service_lock:
        if should_close is not None and should_close():
            return  # a delayed tray caller must not create Tk after quit
        if _service is None:
            _service = _SliderService()
        _service.submit(request)
    request.done.wait()
    if request.error is not None:
        raise request.error


def shutdown_slider(timeout: float = 5.0) -> None:
    """Close any popup and release Tk on its owner thread before process exit."""
    global _service
    with _service_lock:
        service = _service
    if service is None:
        return
    service.shutdown(timeout)
    with _service_lock:
        if _service is service:
            _service = None
