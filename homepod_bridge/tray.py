"""System-tray app (pystray) over :class:`BridgeEngine`.

The menu is described by a pure ``build_menu_spec`` (unit-tested) and then
converted to pystray objects by a thin adapter. Run with
``python -m homepod_bridge tray`` (console, shows logs) or
``pythonw -m homepod_bridge tray`` (no console window).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .config import BridgeConfig, ConfigStore
from .engine import BridgeEngine, EngineState, Snapshot
from .single_instance import SingleInstance

logger = logging.getLogger(__name__)

__all__ = ["TrayApp", "run_tray", "build_menu_spec", "status_text", "make_icon_image"]

STATE_COLORS = {
    EngineState.IDLE: (107, 114, 128),  # gray
    EngineState.CONNECTING: (245, 158, 11),  # amber
    EngineState.STREAMING: (34, 197, 94),  # green
}


# --------------------------------------------------------------------- icon
def make_icon_image(color: Tuple[int, int, int], size: int = 64):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 2, size - 2), radius=size // 5, fill=(*color, 255))
    white = (255, 255, 255, 255)
    d.polygon([(14, 26), (24, 26), (34, 16), (34, 48), (24, 38), (14, 38)], fill=white)
    d.arc((36, 22, 48, 42), start=-50, end=50, fill=white, width=3)
    d.arc((40, 15, 58, 49), start=-50, end=50, fill=white, width=3)
    return img


# ---------------------------------------------------------- pure menu model
@dataclass(frozen=True)
class Item:
    label: str
    action: Optional[str] = None  # action id dispatched by TrayApp
    checked: Optional[bool] = None
    enabled: bool = True
    default: bool = False
    separator: bool = False
    children: Tuple["Item", ...] = field(default_factory=tuple)


SEP = Item(label="-", separator=True)


def status_text(s: Snapshot) -> str:
    if s.state is EngineState.STREAMING:
        return f"Streaming to {len(s.connected)} of {len(s.selected)} device(s)"
    if s.state is EngineState.CONNECTING:
        return "Connecting..."
    return "Idle"


#: A wall clock jump this much larger than elapsed *unsuspended* time means
#: the machine was asleep. Well above any scheduling hiccup.
RESUME_DRIFT_SECONDS = 20.0
RESUME_POLL_SECONDS = 2.0


def awake_seconds() -> float:
    """Elapsed time EXCLUDING any suspend, as a float of seconds.

    ``time.monotonic()`` is no use for spotting a sleep here: CPython
    implements it with QueryPerformanceCounter, which keeps counting across
    S3, so comparing it against the wall clock shows no jump at all (that
    mistake shipped in 0.12.0 and never fired). QueryUnbiasedInterruptTime
    is the documented clock that stops while suspended.
    """
    if os.name == "nt":
        try:
            import ctypes

            value = ctypes.c_ulonglong()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)):
                return value.value / 1e7  # 100ns units
        except Exception:  # noqa: BLE001 - fall through to monotonic
            pass
    return time.monotonic()

#: Receiver-hold presets offered in the tray, with what each is good for.
LATENCY_CHOICES = (
    (0.25, " - tightest"),
    (0.4, ""),
    (0.5, " - default"),
    (1.0, ""),
    (1.5, " - most stable"),
)


def build_menu_spec(
    s: Snapshot, autoconnect: bool, latency: float = 0.5
) -> List[Item]:
    active = s.state is not EngineState.IDLE
    items: List[Item] = [Item(label=status_text(s), enabled=False), SEP]

    # default=True makes a single left-click of the icon fire this item.
    # Unlike v0.5.0 this is now safe: the engine thread never rebuilds the
    # menu (icon color/tooltip only), and every HMENU rebuild - including the
    # one pystray's Icon.__call__ does right after a left-click - is
    # serialized through the update_menu lock installed in TrayApp.run.
    # default is gated to enabled because pystray fires a default item even
    # while it is disabled (Menu.__call__ never checks enabled).
    can_toggle = active or bool(s.selected)
    items.append(
        Item(
            label="Disconnect" if active else "Connect",
            action="toggle_stream",
            enabled=can_toggle,
            default=can_toggle,
        )
    )

    dev_children: List[Item] = [
        Item(label=s.device_label(d.identifier), action=f"toggle:{d.identifier}",
             checked=(d.identifier in s.selected))
        for d in s.devices
    ] or [Item(label="No streamable devices found", enabled=False)]
    dev_children += [SEP, Item(label="Rescan", action="rescan")]
    items.append(Item(label="Devices", children=tuple(dev_children)))

    # With exactly one device selected, its effective level (an override may
    # differ from the master) is what the user is actually hearing.
    vol = s.volumes[0][1] if len(s.selected) == 1 and s.volumes else s.volume
    items.append(Item(label=f"Volume ({int(vol)}%)...", action="volume_slider"))

    # Audio delay: the receiver-side buffer is the bulk of total latency.
    # Lower is tighter for video; too low breaks up on a busy network.
    delay_children = [
        Item(
            label=f"{int(seconds * 1000)} ms{note}",
            action=f"latency:{seconds}",
            checked=abs(latency - seconds) < 0.001,
        )
        for seconds, note in LATENCY_CHOICES
    ]
    items.append(
        Item(label=f"Audio delay ({int(latency * 1000)} ms)", children=tuple(delay_children))
    )

    items += [
        SEP,
        Item(label="Auto-connect at launch", action="autoconnect", checked=autoconnect),
        Item(label="Open logs", action="open_logs"),
        Item(label="Quit", action="quit"),
    ]
    return items


# ------------------------------------------------------------ pystray glue
class TrayApp:
    def __init__(
        self,
        engine: Optional[BridgeEngine] = None,
        store: Optional[ConfigStore] = None,
        slider_factory=None,
    ) -> None:
        if slider_factory is None:
            from .volume_slider import open_slider as slider_factory
        self._slider_factory = slider_factory
        self._slider_open = threading.Event()
        self._slider_thread: Optional[threading.Thread] = None
        self._quitting = threading.Event()
        # Scans run outside this lock. Only applying their results and user
        # session commands are serialized, so Disconnect can invalidate an
        # in-flight scan immediately and its late result cannot reconnect.
        self._action_lock = threading.RLock()
        self._autoconnect_cancel = threading.Event()
        self._autoconnect_thread: Optional[threading.Thread] = None
        self._manual_disconnect = False
        self._save_lock = threading.Lock()  # message thread vs slider thread
        self.store = store or ConfigStore()
        self.cfg: BridgeConfig = self.store.load()
        # Must precede any pyatv connection: shortens the receiver-side
        # buffer pyatv otherwise hardcodes at 1.5s.
        from .raop_latency import apply as apply_raop_latency

        apply_raop_latency(self.cfg.raop_latency)
        self.engine = engine or BridgeEngine(
            on_change=self._on_change,
            on_alert=self._notify_alert,
            volume=self.cfg.volume,
            device_volumes=self.cfg.device_volumes,
            bitrate=self.cfg.bitrate,
            quality=self.cfg.quality,
        )
        self.icon = None
        self._last: Snapshot = self.engine.snapshot()

    def _notify_alert(self, message: str) -> None:
        if self.icon is None:
            return
        try:
            # Windows already shows "HomePod Bridge" as the toast header
            # (see app_identity), so the bold line carries the situation.
            self.icon.notify(message, "Streaming stopped")
        except Exception:  # noqa: BLE001 - notifications are best-effort
            logger.debug("notify failed", exc_info=True)

    # engine callback - may fire from the engine loop thread
    def _on_change(self, snap: Snapshot) -> None:
        prev, self._last = self._last, snap
        self._refresh_icon(snap)
        if (
            snap.state is EngineState.STREAMING
            and prev.state is not EngineState.STREAMING
            and self.icon is not None
        ):
            try:
                self.icon.notify(", ".join(snap.device_label(d) for d in snap.connected), "Streaming")
            except Exception:  # noqa: BLE001 - notifications are best-effort
                logger.debug("notify failed", exc_info=True)

    def _refresh_icon(self, snap: Snapshot) -> None:
        if self.icon is None:
            return
        try:
            # Icon image + tooltip only (Shell_NotifyIcon modify - no HMENU).
            # Menu content is pulled lazily via _menu_items and rebuilt only
            # on the icon's own thread; touching it here from the engine
            # thread is what crashed v0.5.0 on a single icon click.
            self.icon.icon = make_icon_image(STATE_COLORS[snap.state])
            self.icon.title = f"HomePod Bridge - {status_text(snap)}"
        except Exception:  # noqa: BLE001 - UI refresh must never crash the app
            logger.exception("tray refresh failed")

    def _convert_items(self, items):
        import pystray

        # pystray validates callables via __code__.co_argcount (defaults count!)
        # and only accepts 0, 1 or 2 parameters — so bind values with factory
        # closures, never with default-argument lambdas.
        def make_action(action_id: Optional[str]):
            if action_id is None:
                def noop():
                    return None

                return noop

            def handler():
                self.dispatch(action_id)

            return handler

        def make_checked(value: Optional[bool]):
            if value is None:
                return None

            def checked(item):
                return value

            return checked

        out = []
        for it in items:
            if it.separator:
                out.append(pystray.Menu.SEPARATOR)
            elif it.children:
                out.append(
                    pystray.MenuItem(
                        it.label, pystray.Menu(*self._convert_items(it.children))
                    )
                )
            else:
                out.append(
                    pystray.MenuItem(
                        it.label,
                        make_action(it.action),
                        checked=make_checked(it.checked),
                        enabled=it.enabled,
                        default=it.default,
                    )
                )
        return out

    def _menu_items(self):
        """Lazy menu source - evaluated by pystray on ITS thread when the
        HMENU is (re)built, always reflecting the latest snapshot."""
        return self._convert_items(
            build_menu_spec(self._last, self.cfg.autoconnect, self.cfg.raop_latency)
        )

    def _pystray_menu(self, snap: Snapshot):
        import pystray

        return pystray.Menu(
            *self._convert_items(
                build_menu_spec(snap, self.cfg.autoconnect, self.cfg.raop_latency)
            )
        )

    def _request_menu_refresh(self) -> None:
        """Rebuild the HMENU. Only call from pystray-owned threads (menu item
        handlers run on the icon's message thread after the menu closed)."""
        if self.icon is None:
            return
        try:
            self.icon.update_menu()
        except Exception:  # noqa: BLE001
            logger.debug("menu refresh failed", exc_info=True)

    def _save(self) -> None:
        try:
            with self._save_lock:
                self.store.save(self.cfg)
        except Exception:  # noqa: BLE001
            logger.exception("config save failed")

    def dispatch(self, action: str) -> None:
        try:
            if action == "toggle_stream":
                self._cancel_autoconnect()
                with self._action_lock:
                    # State-aware so a stale menu label still does the right thing.
                    if self.engine.snapshot().state is EngineState.IDLE:
                        self._manual_disconnect = False
                        self.engine.start_selected()
                    else:
                        self._manual_disconnect = True
                        self.engine.stop_streaming()
            elif action == "rescan":
                threading.Thread(target=self._rescan_devices, name="device-scan", daemon=True).start()
            elif action.startswith("toggle:"):
                self._cancel_autoconnect()
                with self._action_lock:
                    snap = self.engine.toggle_device(action[len("toggle:"):])
                    self.cfg.devices = list(snap.selected)
                    self.cfg.legacy_devices.clear()
                    self._save()
            elif action == "volume_slider":
                self._open_volume_slider()
            elif action == "open_logs":
                from .logging_setup import log_directory

                path = log_directory()
                path.mkdir(parents=True, exist_ok=True)
                if hasattr(os, "startfile"):  # Windows
                    os.startfile(path)  # type: ignore[attr-defined]
                else:
                    logger.info("logs at %s", path)
            elif action.startswith("latency:"):
                from .raop_latency import set_latency

                self.cfg.raop_latency = set_latency(float(action.split(":", 1)[1]))
                self._save()
                # The hold is read when a session starts, so reconnect to
                # apply it - otherwise the change appears to do nothing.
                with self._action_lock:
                    if self.engine.snapshot().state is not EngineState.IDLE:
                        self.engine.stop_streaming()
                        self.engine.start_selected()
            elif action == "autoconnect":
                self._cancel_autoconnect()
                with self._action_lock:
                    self.cfg.autoconnect = not self.cfg.autoconnect
                    if self.cfg.autoconnect:
                        self._manual_disconnect = False
                    self._save()
                    self._refresh_icon(self._last)
            elif action == "quit":
                self.quit()
                return  # icon is stopping; no refresh
        except Exception:  # noqa: BLE001 - menu actions must never crash the tray
            logger.exception("action %r failed", action)
            if self.icon is not None:
                try:
                    self.icon.notify("See Open logs for details.", "Action failed")
                except Exception:  # noqa: BLE001
                    pass
        finally:
            # Runs on the icon's message thread (menu already closed) - the
            # one safe place to rebuild the HMENU with fresh state.
            self._request_menu_refresh()

    def _open_volume_slider(self) -> None:
        if self._slider_open.is_set():
            return  # one popup at a time
        self._slider_open.set()
        snap = self._last
        # Per-room rows only when the mix can actually differ; a single
        # device (or stereo pair endpoint) keeps the classic one-slider UI.
        row_identifiers = {snap.device_label(identifier): identifier for identifier, _ in snap.volumes}
        device_rows = [(snap.device_label(identifier), volume) for identifier, volume in snap.volumes] if len(snap.selected) >= 2 else []
        initial = snap.volume
        if len(snap.selected) == 1 and snap.volumes:
            # Single device: show ITS effective level - the master could be
            # hiding an override, and dragging from the wrong number would
            # jump the room's volume audibly.
            initial = snap.volumes[0][1]
        # None = master; device names are always str, so no collision.
        sent_log: List[Tuple[Optional[str], float]] = []

        # Fire-and-forget sends: a blocking engine round-trip on the flyout's
        # tkinter thread freezes it whenever the engine is busy, and the 20s
        # call timeout used to close the flyout mid-drag.
        def send(v: float) -> None:
            sent_log.append((None, float(v)))
            self.engine.set_volume_nowait(v)

        def send_device(name: str, v: float) -> None:
            identifier = row_identifiers[name]
            sent_log.append((identifier, float(v)))
            self.engine.set_device_volume_nowait(identifier, v)

        def run() -> None:
            try:
                self._slider_factory(
                    initial=initial,
                    set_volume=send,
                    title="HomePods",
                    should_close=self._quitting.is_set,
                    devices=device_rows,
                    set_device_volume=send_device,
                )
            except Exception:  # noqa: BLE001 - a broken popup must not kill the tray
                logger.exception("volume slider failed")
            finally:
                try:
                    if sent_log:
                        # Replay in send order so master-vs-device precedence
                        # matches what the engine actually ended up with
                        # (master clears all overrides).
                        with self._action_lock:
                            volume = self.cfg.volume
                            overrides = dict(self.cfg.device_volumes)
                            for name, v in sent_log:
                                v = max(0.0, min(100.0, v))
                                if name is None:  # master: sets all, clears overrides
                                    volume, overrides = v, {}
                                    self.cfg.legacy_device_volumes.clear()
                                else:
                                    overrides[name] = v
                            self.cfg.volume = volume
                            self.cfg.device_volumes = overrides
                            self._save()
                except Exception:  # noqa: BLE001 - engine may be shut down
                    pass
                finally:
                    self._slider_open.clear()

        self._slider_thread = threading.Thread(
            target=run, name="volume-slider", daemon=True
        )
        self._slider_thread.start()

    def _setup(self, icon) -> None:
        # Runs on pystray's setup thread - it must NOT rebuild the menu:
        # a right-click during the startup scan would have DestroyMenu run
        # against the HMENU being displayed (the v0.5.0 crash class). The
        # pre-display refresh in run() makes an explicit rebuild unnecessary.
        icon.visible = True
        cancel = self._autoconnect_cancel
        try:
            snap = self.engine.rescan()
            with self._action_lock:
                if not cancel.is_set() and not self._quitting.is_set():
                    snap = self._restore_saved_selection(snap)
                    self._refresh_icon(snap)
        except Exception:  # noqa: BLE001
            logger.exception("startup scan failed")
        if not cancel.is_set() and self.cfg.autoconnect and (self.cfg.devices or self.cfg.legacy_devices):
            self._autoconnect_with_retry()
        threading.Thread(
            target=self._watch_for_resume, name="resume-watch", daemon=True
        ).start()

    def _cancel_autoconnect(self) -> None:
        self._autoconnect_cancel.set()

    def _rescan_devices(self) -> None:
        try:
            self.engine.rescan()
        except Exception:  # noqa: BLE001 - background action must not crash
            logger.exception("device scan failed")

    def _restore_saved_selection(self, snap: Snapshot) -> Snapshot:
        previous_volumes = dict(self.cfg.device_volumes)
        if self.cfg.resolve_legacy(snap.devices):
            for identifier, volume in self.cfg.device_volumes.items():
                if previous_volumes.get(identifier) != volume:
                    self.engine.set_device_volume(identifier, volume)
            self._save()
        return self.engine.set_selected(self.cfg.devices)

    def _autoconnect_with_retry(self, attempts: int = 12, delay: float = 10.0) -> None:
        """Keep trying to connect at launch until the network is ready.

        When started at login, Wi-Fi/mDNS often come up *after* us; a single
        failed scan would leave the app idle forever. Retry for ~2 minutes;
        once a session starts, the per-device watchdogs take over.
        """

        with self._action_lock:
            if self._quitting.is_set() or self._manual_disconnect or not self.cfg.autoconnect:
                return
            self._cancel_autoconnect()
            cancel = self._autoconnect_cancel = threading.Event()

        def cancelled() -> bool:
            return cancel.is_set() or self._quitting.is_set()

        def run() -> None:
            for attempt in range(attempts):
                if cancelled():
                    return
                try:
                    snap = self.engine.rescan()
                    with self._action_lock:
                        if cancelled() or not self.cfg.autoconnect or self._manual_disconnect:
                            return
                        if self.engine.snapshot().state is not EngineState.IDLE:
                            return  # a manual connect already started a session
                        snap = self._restore_saved_selection(snap)
                        if cancelled():
                            return
                        if {d.identifier for d in snap.devices}.intersection(snap.selected):
                            self.engine.start_selected()
                        if self.engine.snapshot().state is not EngineState.IDLE:
                            logger.info("auto-connect succeeded on attempt %d", attempt + 1)
                            return
                except Exception:  # noqa: BLE001
                    logger.exception("auto-connect attempt %d failed", attempt + 1)
                if cancel.wait(delay) or self._quitting.is_set():
                    return
            logger.warning("auto-connect gave up after %d attempts", attempts)

        self._autoconnect_thread = threading.Thread(target=run, name="auto-connect", daemon=True)
        self._autoconnect_thread.start()

    def run(self) -> int:
        import pystray

        icon_cls = pystray.Icon
        if hasattr(icon_cls, "_on_notify"):  # win32 backend
            app = self
            WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205

            class _FreshMenuIcon(icon_cls):
                """Rebuild the HMENU on the message thread right before it is
                displayed, so the menu always shows the current engine state.
                Without this, engine-driven transitions (autoconnect, capture
                give-up) left a stale Connect/Disconnect label whose click
                performed the inverted action.

                Reentrancy guard: TrackPopupMenuEx runs a modal message loop
                that dispatches queued clicks to this handler while the menu
                is on screen. Rebuilding then would DestroyMenu the menu
                being displayed (the v0.5.0 crash class), so queued clicks
                arriving inside the modal loop are ignored."""

                _in_notify = False

                def _on_notify(self, wparam, lparam):
                    if lparam in (WM_LBUTTONUP, WM_RBUTTONUP):
                        if self._in_notify:
                            return
                        self._in_notify = True
                        try:
                            if lparam == WM_RBUTTONUP:
                                app._request_menu_refresh()
                            super()._on_notify(wparam, lparam)
                        finally:
                            self._in_notify = False
                    else:
                        super()._on_notify(wparam, lparam)

            icon_cls = _FreshMenuIcon

        self.icon = icon_cls(
            "homepod-bridge",
            make_icon_image(STATE_COLORS[EngineState.IDLE]),
            "HomePod Bridge",
            pystray.Menu(self._menu_items),  # lazy: resolved on the icon thread
        )
        # Serialize every HMENU rebuild (ours in dispatch/setup and pystray's
        # own in Icon.__call__ after a left-click) through one lock.
        original_update = self.icon.update_menu
        update_lock = threading.Lock()

        def locked_update_menu():
            with update_lock:
                original_update()

        self.icon.update_menu = locked_update_menu
        self.icon.run(setup=self._setup)
        return 0

    def _watch_for_resume(self) -> None:
        """Reconnect after the machine wakes from sleep.

        Nothing else notices: RAOP audio is UDP, so sending to a HomePod
        that vanished never errors, and the capture thread keeps feeding
        silence, so pyatv's stream never ends and the watchdog never fires.
        The tray sits in STREAMING with a session that died hours ago -
        observed as five hours of total log silence across a sleep.
        """
        logger.info("watching for sleep/resume")
        last_wall, last_awake = time.time(), awake_seconds()
        while not self._quitting.wait(RESUME_POLL_SECONDS):
            wall, awake = time.time(), awake_seconds()
            drift = (wall - last_wall) - (awake - last_awake)
            last_wall, last_awake = wall, awake
            if drift < RESUME_DRIFT_SECONDS:
                continue
            logger.warning(
                "system resumed after ~%.0fs suspended; restarting session", drift
            )
            try:
                with self._action_lock:
                    if self._quitting.is_set() or self._manual_disconnect:
                        continue
                    if self.engine.snapshot().state is not EngineState.IDLE:
                        self.engine.stop_streaming()
                        self.engine.start_selected()
                    elif self.cfg.autoconnect and (self.cfg.devices or self.cfg.legacy_devices):
                        self._autoconnect_with_retry()
            except Exception:  # noqa: BLE001 - never kill the watcher
                logger.exception("reconnect after resume failed")

    def quit(self) -> None:
        from .volume_slider import shutdown_slider

        self._quitting.set()
        self._cancel_autoconnect()
        try:
            # Flush pending volume changes while the engine is still alive;
            # Tk and its interpreter are released on their owning thread.
            shutdown_slider()
        except Exception:  # noqa: BLE001 - finish other cleanup on UI failure
            logger.exception("volume UI shutdown failed")
        finally:
            thread = self._slider_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)  # save the final popup values
            try:
                with self._action_lock:
                    self.engine.shutdown()
            finally:
                if self.icon is not None:
                    self.icon.stop()


def _already_running_dialog() -> None:
    try:
        from tkinter import Tk, messagebox

        root = Tk()
        root.withdraw()
        messagebox.showinfo(
            "HomePod Bridge",
            "HomePod Bridge is already running - check the system tray.",
        )
        root.destroy()
    except Exception:  # noqa: BLE001 - headless or tk unavailable
        logger.warning("another instance is already running")


def run_tray() -> int:
    # The homepod-bridge-tray gui-script points here directly; without this,
    # a windowless run has no handlers and every log record vanishes.
    from .app_identity import ensure_windows_identity
    from .logging_setup import logging_configured, setup_logging

    if not logging_configured():
        setup_logging(filename="bridge-tray.log")
    # Before any UI: makes notifications say "HomePod Bridge" instead of
    # "Python" (the host interpreter's file description).
    ensure_windows_identity()
    guard = SingleInstance()
    if not guard.acquire():
        _already_running_dialog()
        return 1
    app = None
    try:
        app = TrayApp()
        return app.run()
    finally:
        try:
            if app is not None:
                app.quit()
        finally:
            guard.release()
