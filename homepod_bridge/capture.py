"""WASAPI loopback capture of the default output device (Windows only).

Uses PyAudioWPatch, which exposes true WASAPI loopback devices - no virtual
cable driver required. Import of ``pyaudiowpatch`` is deferred so the rest of
the package (and the test suite) works on any OS.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["CaptureFormat", "LoopbackCapture"]


def _default_render_endpoint_id() -> Optional[str]:
    """Device ID of the CURRENT default render endpoint, via Core Audio COM.

    PortAudio cannot answer this: Pa_Initialize is refcounted process-wide
    and the device table is built only when the count goes 0->1, so while
    the capture's own PyAudio instance is alive, any 'fresh' PyAudio in this
    process sees the same frozen snapshot and never observes a change.
    """
    if sys.platform != "win32":
        return None
    import ctypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("d1", ctypes.c_ulong),
            ("d2", ctypes.c_ushort),
            ("d3", ctypes.c_ushort),
            ("d4", ctypes.c_ubyte * 8),
        ]

    try:
        ole32 = ctypes.WinDLL("ole32")
    except OSError:  # pragma: no cover
        return None
    hr_init = ole32.CoInitializeEx(None, 0)  # MTA; S_FALSE if already init
    try:
        def guid(s: str) -> _GUID:
            g = _GUID()
            if ole32.CLSIDFromString(s, ctypes.byref(g)) != 0:
                raise OSError(f"bad GUID {s}")
            return g

        def com_method(obj, index: int, *argtypes):
            vtbl = ctypes.cast(
                obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
            ).contents
            proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
            return proto(vtbl[index])

        clsid = guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")  # MMDeviceEnumerator
        iid = guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")  # IMMDeviceEnumerator
        enumerator = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid),
            None,
            1,  # CLSCTX_INPROC_SERVER
            ctypes.byref(iid),
            ctypes.byref(enumerator),
        )
        if hr != 0 or not enumerator:
            return None
        try:
            # IMMDeviceEnumerator vtable: QI(0) AddRef(1) Release(2)
            # EnumAudioEndpoints(3) GetDefaultAudioEndpoint(4) ...
            device = ctypes.c_void_p()
            get_default = com_method(
                enumerator,
                4,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
            )
            hr = get_default(enumerator, 0, 0, ctypes.byref(device))  # eRender/eConsole
            if hr != 0 or not device:
                return None  # e.g. no audio device present
            try:
                # IMMDevice vtable: QI(0) AddRef(1) Release(2) Activate(3)
                # OpenPropertyStore(4) GetId(5) ...
                pid = ctypes.c_wchar_p()
                get_id = com_method(device, 5, ctypes.POINTER(ctypes.c_wchar_p))
                if get_id(device, ctypes.byref(pid)) != 0 or pid.value is None:
                    return None
                endpoint_id = str(pid.value)
                ole32.CoTaskMemFree(pid)
                return endpoint_id
            finally:
                com_method(device, 2)(device)  # IUnknown::Release
        finally:
            com_method(enumerator, 2)(enumerator)  # IUnknown::Release
    except Exception:  # noqa: BLE001 - probe is best-effort
        return None
    finally:
        if hr_init in (0, 1):  # S_OK / S_FALSE must be balanced
            ole32.CoUninitialize()

CHUNK_FRAMES = 1024  # frames per read (~21ms @ 48kHz)
DEVICE_POLL_SECONDS = 2.0  # how often to check for a default-device switch
#: No real frames for this long means the machine is genuinely silent.
#: Must exceed the normal read gap (WASAPI packets make it 20-30ms).
IDLE_GRACE_SECONDS = 0.20
#: Consecutive failed device probes before the endpoint counts as gone
#: rather than a transient COM hiccup.
DEVICE_MISSING_POLLS = 2


@dataclass(frozen=True)
class CaptureFormat:
    sample_rate: int
    channels: int
    sample_width: int = 2  # int16


class LoopbackCapture:
    """Captures 'what you hear' from the default output device.

    A reader thread pushes raw interleaved int16 PCM chunks into ``sink``
    (typically a :class:`~homepod_bridge.mp3_pipe.SinkSwitch`).
    """

    def __init__(
        self,
        sink: Callable[[bytes], None],
        on_failure: Optional[Callable[[], None]] = None,
        device_probe: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        try:
            import pyaudiowpatch as pyaudio
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise RuntimeError(
                "PyAudioWPatch is required for loopback capture and is "
                "Windows-only. Install with: pip install PyAudioWPatch"
            ) from exc
        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()
        self._sink = sink
        self._on_failure = on_failure
        self._stream = None
        self._thread: Optional[threading.Thread] = None
        self._watcher: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._device_changed = False
        self._poll_interval = DEVICE_POLL_SECONDS
        self._probe = device_probe or _default_render_endpoint_id
        self._baseline_ref: Optional[str] = None
        try:
            self._device = self._find_loopback_device()
        except Exception:
            self._pa.terminate()
            self._pa = None
            raise
        self.fmt = CaptureFormat(
            sample_rate=int(self._device["defaultSampleRate"]),
            channels=max(1, int(self._device["maxInputChannels"])),
        )

    def _find_loopback_device(self) -> dict:
        pa, pyaudio = self._pa, self._pyaudio
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if default_out.get("isLoopbackDevice"):
            return default_out
        for dev in pa.get_loopback_device_info_generator():
            if default_out["name"] in dev["name"]:
                return dev
        raise RuntimeError(
            "No WASAPI loopback device found for output "
            f"'{default_out['name']}'. Is audio playing on the default device?"
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        pyaudio = self._pyaudio
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.fmt.channels,
            rate=self.fmt.sample_rate,
            frames_per_buffer=CHUNK_FRAMES,
            input=True,
            input_device_index=self._device["index"],
        )
        self._stop.clear()
        self._baseline_ref = self._probe()
        self._thread = threading.Thread(
            target=self._run, name="loopback-capture", daemon=True
        )
        self._thread.start()
        self._watcher = threading.Thread(
            target=self._watch_default_device, name="device-watch", daemon=True
        )
        self._watcher.start()
        logger.info(
            "Capturing '%s' @ %d Hz, %d ch",
            self._device["name"],
            self.fmt.sample_rate,
            self.fmt.channels,
        )

    def _watch_default_device(self) -> None:
        """Restart capture when the output device changes OR goes away.

        Neither shows up as an error: the old endpoint keeps returning no
        frames, the read loop treats that as an idle machine and injects
        silence, and the bridge streams silence forever.

        The disappearing case is the common one on a desktop, because the
        default output is often the monitor's HDMI audio - when the display
        enters standby Windows removes that endpoint entirely, and restores
        it with the SAME id on wake. Comparing ids alone therefore sees
        nothing, so an absence followed by a return has to count as a
        change in its own right.
        """
        baseline = self._baseline_ref
        missing = 0
        while not self._stop.wait(self._poll_interval):
            current = self._probe()
            if current is None:
                # One failure is noise; a sustained one means the endpoint
                # is genuinely gone (display asleep, device unplugged).
                missing += 1
                continue
            if baseline is None:
                baseline = current  # probe failed at start(); anchor now
                missing = 0
                continue
            if missing >= DEVICE_MISSING_POLLS:
                logger.warning(
                    "output device returned after %.0fs away; restarting capture",
                    missing * self._poll_interval,
                )
                self._device_changed = True
                return
            missing = 0
            if current != baseline:
                logger.warning("default output device changed; restarting capture")
                self._device_changed = True
                return

    def _run(self) -> None:
        failed = False
        period = CHUNK_FRAMES / float(self.fmt.sample_rate)  # ~21ms
        silence = b"\x00" * (CHUNK_FRAMES * self.fmt.channels * self.fmt.sample_width)
        now = time.monotonic()
        last_data = now  # last time the device handed us REAL frames
        # End time of the next output chunk. Keep this sample clock across
        # reads: resetting it to wall time loses every short silent gap.
        next_due = now + period
        while not self._stop.is_set():
            if self._device_changed:
                failed = True
                break
            try:
                # Never block inside read(): WASAPI loopback delivers NO
                # frames while the machine is silent, so a blocking read
                # would hang past stop()'s join timeout (forcing the leak
                # path on every disconnect with audio paused).
                available = self._stream.get_read_available()
                now = time.monotonic()
                queued_seconds = available / float(self.fmt.sample_rate)
                missing_seconds = now - (next_due - period) - queued_seconds
                if missing_seconds > 1.0:
                    # A suspended/stalled reader must not flood the sink
                    # with historical silence. Preserve queued real audio,
                    # and resume its timeline at the current wall clock.
                    next_due = now + period - queued_seconds
                    missing_seconds = 0.0
                if available >= CHUNK_FRAMES and missing_seconds >= 2 * period:
                    # Playback can resume before IDLE_GRACE_SECONDS. Fill
                    # its missing time BEFORE the new audio, retaining one
                    # chunk of timing slack for packet arrival jitter. The
                    # queued frames above already cover elapsed time and
                    # must never be replaced by additional silence.
                    data = silence
                    next_due += period
                elif available >= CHUNK_FRAMES:
                    data = self._stream.read(CHUNK_FRAMES, exception_on_overflow=False)
                    last_data = now
                    next_due += period
                else:
                    # WASAPI hands over 480-frame packets every 10ms, and 480
                    # does not divide CHUNK_FRAMES, so "no full chunk yet" is
                    # NORMAL mid-playback: reads land every 20-30ms around a
                    # 21.3ms mean. Injecting silence on a per-chunk deadline
                    # therefore fires ~6x/s WHILE MUSIC PLAYS, producing 13%
                    # more audio than realtime. RAOP drains at realtime only,
                    # so that phantom audio becomes permanent latency and
                    # then continuous dropouts. Only a real gap in delivery
                    # means the machine has actually gone quiet.
                    idle = now - last_data >= IDLE_GRACE_SECONDS
                    due = next_due + queued_seconds
                    if not idle or now < due:
                        self._stop.wait(
                            0.005 if not idle else min(0.005, due - now)
                        )
                        continue
                    # Genuine silence: feed silence at the capture rate, or
                    # the stream starves and the HomePod drops the session.
                    data = silence
                    next_due += period
                # The sink chain (encoder -> buffer) must be inside the try:
                # an escaping exception would kill this thread WITHOUT firing
                # on_failure, silently ending audio while the tray stays green.
                self._sink(data)
            except Exception:  # noqa: BLE001 - capture boundary
                if not self._stop.is_set():
                    logger.exception("Capture failed; capture thread exiting")
                    failed = True
                break
        if failed and self._on_failure is not None:
            try:
                self._on_failure()  # e.g. after sleep/resume or device removal
            except Exception:  # noqa: BLE001
                logger.exception("capture failure callback raised")

    def stop(self) -> None:
        self._stop.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.join(timeout=1)  # wakes from Event.wait immediately
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
            if thread.is_alive():
                # The reader is stuck inside a native read (driver hang, e.g.
                # around sleep). Closing the stream or terminating PortAudio
                # under it is a cross-thread use-after-close that can crash
                # the process natively - deliberately leak instead.
                logger.warning("capture thread did not exit; leaking its stream")
                self._stream = None
                self._pa = None  # never terminate under the stuck reader
                return
        stream, self._stream = self._stream, None
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:  # noqa: BLE001 - device may already be gone
            logger.warning("capture stream teardown failed", exc_info=True)
        finally:
            pa, self._pa = self._pa, None
            if pa is not None:
                pa.terminate()
