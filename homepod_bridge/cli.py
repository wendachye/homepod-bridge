"""homepod-bridge CLI.

Commands:
    scan                     List AirPlay devices and whether they're streamable.
    stream --device NAME     Stream Windows system audio to one or more devices
                             (repeat --device for multi-room).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Dict, List, Optional

from .airplay import (
    ACCESS_HINT,
    DeviceInfo,
    RetryPolicy,
    StreamStats,
    pick_devices,
    scan_devices,
    stream_forever,
)
from .engine import LIVE_BUFFER_SECONDS
from .mp3_pipe import FanOutSink, Mp3Pipe, SinkSwitch
from .pcm_pipe import PcmPipe
from .stream_buffer import StreamBuffer

logger = logging.getLogger("homepod_bridge")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="homepod-bridge",
        description="Stream Windows system audio to HomePods over AirPlay.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    sp_scan = sub.add_parser("scan", help="list AirPlay devices on the network")
    sp_scan.add_argument("--timeout", type=int, default=6, help="scan seconds")

    sp_stream = sub.add_parser("stream", help="stream system audio to device(s)")
    sp_stream.add_argument(
        "--device",
        action="append",
        required=True,
        dest="devices",
        metavar="NAME",
        help='device name; repeat for multiple, e.g. --device "Living Room" --device "Living Room (2)"',
    )
    sp_stream.add_argument("--bitrate", type=int, default=320, help="MP3 kbps (unused: audio is sent as raw PCM)")
    sp_stream.add_argument("--quality", type=int, default=2, help="LAME quality 0-9 (unused)")
    sp_stream.add_argument("--scan-timeout", type=int, default=6)
    sp_stream.add_argument(
        "--latency",
        type=float,
        default=None,
        help="receiver buffer in seconds (default 0.5; raise if audio breaks up)",
    )

    sub.add_parser("tray", help="run the system-tray app (Windows)")
    return p


def _print_devices(devices: List[DeviceInfo]) -> None:
    if not devices:
        print("No AirPlay devices found. Check Wi-Fi network/band and that the")
        print("Windows Firewall allows Python on private networks (mDNS).")
        return
    width = max(len(d.name) for d in devices)
    for d in devices:
        verdict = "OK: streamable" if d.streamable else "NOT streamable"
        print(f"{d.name:<{width}}  {d.address:<15}  RAOP={d.pairing_label:<11} {verdict}")
    if any(not d.streamable for d in devices):
        print(f"\nHint: {ACCESS_HINT}")


def cmd_scan(args: argparse.Namespace) -> int:
    devices = asyncio.run(scan_devices(timeout=args.timeout))
    _print_devices(devices)
    return 0


async def _run_stream(
    args: argparse.Namespace,
    switches: List[SinkSwitch],
    fmt,
    failure_holder: Optional[dict] = None,
) -> int:
    devices = await scan_devices(timeout=args.scan_timeout)
    try:
        targets = pick_devices(devices, args.devices)
    except LookupError as exc:
        print(f"Error: {exc}")
        _print_devices(devices)
        return 2

    blocked = [t for t in targets if not t.streamable]
    if blocked:
        for t in blocked:
            print(f"'{t.name}' found, but RAOP pairing is '{t.pairing_label}'.")
        print(ACCESS_HINT)
        return 2

    current: Dict[str, Mp3Pipe] = {}
    stop = asyncio.Event()
    capture_died = {"flag": False}
    if failure_holder is not None:
        loop = asyncio.get_running_loop()

        def _capture_failed() -> None:
            capture_died["flag"] = True
            stop.set()
            for pipe in current.values():
                pipe.finish()  # EOF unblocks readers so sessions wind down

        # Fired from the capture thread (read failure or default-device
        # switch). The CLI has no restart machinery, so exit with a clear
        # error instead of hanging on readers that will never see data.
        failure_holder["notify"] = lambda: loop.call_soon_threadsafe(_capture_failed)

    def make_open_reader(target: DeviceInfo, switch: SinkSwitch):
        def open_reader():
            # Fresh encoder + buffer per (re)connect => clean MP3 stream start,
            # scoped to this device only; other sessions are untouched.
            old = current.get(target.identifier)
            if old is not None:
                old.finish()
            byte_rate = fmt.sample_rate * fmt.channels * fmt.sample_width
            live_cap = max(64 * 1024, int(byte_rate * LIVE_BUFFER_SECONDS))
            # Raw PCM: pyatv sends PCM to the device anyway, and an MP3
            # container adds ~1.6s of decoder-init stall.
            pipe = PcmPipe(
                fmt.sample_rate,
                fmt.channels,
                fmt.sample_width,
                buffer=StreamBuffer(max_bytes=live_cap),
            )
            switch.set(pipe.feed_pcm)
            current[target.identifier] = pipe
            return pipe.reader()

        return open_reader

    stats = {t.identifier: StreamStats() for t in targets}
    names = ", ".join(f"'{t.name}'" for t in targets)
    print(f"Streaming system audio to {names}. Ctrl+C to stop.")
    if len(targets) > 1:
        print(
            "Note: multi-device sync is best-effort. Speakers in the same room may\n"
            "have a small audible offset; a Home-app stereo pair gives perfect sync."
        )
    try:
        await asyncio.gather(
            *(
                stream_forever(
                    t.identifier,
                    make_open_reader(t, sw),
                    RetryPolicy(),
                    stop_event=stop,
                    stats=stats[t.identifier],
                )
                for t, sw in zip(targets, switches)
            )
        )
    finally:
        for pipe in current.values():
            pipe.finish()
        for sw in switches:
            sw.set(None)
        for t in targets:
            s = stats[t.identifier]
            logger.info(
                "%s: %d connect(s), %d failure(s)%s",
                t.name,
                s.connects,
                s.failures,
                f", last error: {s.last_error}" if s.last_error else "",
            )
    if capture_died["flag"]:
        print(
            "Audio capture died (output device changed, removed, or sleep). "
            "Re-run the command to resume."
        )
        return 1
    return 0


def cmd_stream(args: argparse.Namespace) -> int:
    from .capture import LoopbackCapture  # Windows-only import, deferred
    from .raop_latency import apply as apply_raop_latency

    apply_raop_latency(args.latency)  # before any connection

    switches = [SinkSwitch() for _ in args.devices]
    failure_holder: Dict[str, object] = {"notify": None}

    def on_capture_failure() -> None:
        notify = failure_holder.get("notify")
        if callable(notify):
            notify()

    try:
        capture = LoopbackCapture(FanOutSink(switches), on_failure=on_capture_failure)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 2
    if capture.fmt.channels > 2:
        # lameenc rejects >2 channels; without this gate the watchdog would
        # flap reconnects forever with no audio and no clear error.
        capture.stop()
        print(
            f"Error: the default output device is {capture.fmt.channels}-channel "
            "(surround), which MP3 encoding does not support. Switch Windows "
            "output to a stereo device and try again."
        )
        return 2
    capture.start()
    try:
        return asyncio.run(_run_stream(args, switches, capture.fmt, failure_holder))
    except KeyboardInterrupt:
        print("\nStopping.")
        return 0
    finally:
        capture.stop()


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    from .logging_setup import setup_logging

    # Per-mode files: a long-running tray and a debugging CLI sharing one
    # file makes Windows rotation fail (open-file rename) and go dark.
    logname = "bridge-tray.log" if args.command == "tray" else "bridge-cli.log"
    setup_logging(verbose=args.verbose, filename=logname)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "stream":
        return cmd_stream(args)
    if args.command == "tray":
        from .tray import run_tray

        return run_tray()
    return 1


if __name__ == "__main__":
    sys.exit(main())
