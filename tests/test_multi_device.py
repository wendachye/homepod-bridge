import time as _time

import pytest as _pytest

from homepod_bridge import engine as _engine_mod
from homepod_bridge.capture import CaptureFormat as _CaptureFormat


def test_resync_trims_a_lagging_room_to_the_live_edge():
    """Devices connect seconds apart and independently discard different
    startup backlogs, so they settle at different offsets - measured 21ms
    typical and up to 320ms between two HomePods, audible as a smear."""
    import asyncio as _asyncio

    from homepod_bridge.engine import BridgeEngine
    from homepod_bridge.pcm_pipe import PcmPipe
    from homepod_bridge.stream_buffer import StreamBuffer

    fmt = _CaptureFormat(sample_rate=48000, channels=2)
    rate = 48000 * 2 * 2

    async def scenario():
        engine = BridgeEngine(
            scan_fn=lambda timeout=6: _asyncio.sleep(0, []),
            stream_fn=lambda *a, **k: None,
            capture_factory=lambda sink, on_failure=None: None,
        )
        try:
            ahead = PcmPipe(48000, 2, 2, buffer=StreamBuffer(max_bytes=rate, align=4))
            behind = PcmPipe(48000, 2, 2, buffer=StreamBuffer(max_bytes=rate, align=4))
            ahead.feed_pcm(b"\x00" * int(rate * 0.30))  # 300ms behind live
            behind.feed_pcm(b"\x00" * int(rate * 0.01))  # nearly live
            engine._pipes = {"A": ahead, "B": behind}
            engine._stop_evt = _asyncio.Event()
            engine._session = object()

            task = _asyncio.get_running_loop().create_task(
                engine._keep_devices_in_sync(fmt)
            )
            await _asyncio.sleep(_engine_mod.RESYNC_INTERVAL * 1.6)
            task.cancel()

            limit = (
                _engine_mod.RESYNC_TARGET_SECONDS
                + _engine_mod.RESYNC_THRESHOLD_SECONDS
            )
            for name, pipe in (("ahead", ahead), ("behind", behind)):
                backlog = pipe.buffer._size / rate
                assert backlog <= limit + 0.005, (
                    f"{name} still {backlog*1000:.0f} ms behind live"
                )
            # standing backlog is permanent added latency, so the laggard is
            # pulled toward live - not merely level with the other room
            assert ahead.buffer.dropped_bytes > 0
            assert behind.buffer.dropped_bytes == 0  # already live, untouched
        finally:
            engine.shutdown()

    _asyncio.run(scenario())


import asyncio
from types import SimpleNamespace

import pytest
from pyatv.const import PairingRequirement

from homepod_bridge import airplay
from homepod_bridge.airplay import (
    DeviceInfo,
    RetryPolicy,
    StreamStats,
    pick_devices,
    stream_forever,
)
from homepod_bridge.cli import build_parser
from homepod_bridge.mp3_pipe import FanOutSink, Mp3Pipe, SinkSwitch


def dev(name: str) -> DeviceInfo:
    return DeviceInfo(
        name=name,
        address="10.0.0.9",
        identifier=f"id-{name}",
        raop_pairing=PairingRequirement.NotNeeded,
    )


# ---------------------------------------------------------------- FanOutSink
def test_fanout_forwards_to_all_sinks_in_order():
    received = {"a": [], "b": []}
    fan = FanOutSink([received["a"].append, received["b"].append])
    fan(b"x")
    fan(b"y")
    assert received["a"] == [b"x", b"y"]
    assert received["b"] == [b"x", b"y"]


def test_fanout_with_sink_switches_feeds_independent_pipes():
    class FakeEncoder:
        def encode(self, pcm):
            return b"E:" + pcm

        def flush(self):
            return b"|"

    s1, s2 = SinkSwitch(), SinkSwitch()
    fan = FanOutSink([s1, s2])
    p1, p2 = Mp3Pipe(FakeEncoder()), Mp3Pipe(FakeEncoder())
    s1.set(p1.feed_pcm)
    s2.set(p2.feed_pcm)

    fan(b"aa")
    s2.set(None)  # device 2 reconnecting: its sink detached, device 1 unaffected
    fan(b"bb")

    p1.finish()
    p2.finish()
    assert p1.reader().read() == b"E:aaE:bb|"
    assert p2.reader().read() == b"E:aa|"


# ------------------------------------------------------------- pick_devices
def test_pick_devices_resolves_users_exact_scenario():
    devices = [
        dev("Living Room (2)"),
        dev("user's MacBook Air"),
        dev("Living Room"),
        dev("Master Bedroom"),
    ]
    targets = pick_devices(devices, ["Living Room", "Living Room (2)"])
    assert [t.name for t in targets] == ["Living Room", "Living Room (2)"]
    assert len({t.identifier for t in targets}) == 2


def test_pick_devices_rejects_same_device_twice():
    devices = [dev("Living Room"), dev("Living Room (2)")]
    with pytest.raises(LookupError, match="more than once"):
        pick_devices(devices, ["Living Room", "living room"])


# ---------------------------------------------------------------- CLI parse
def test_cli_accepts_repeated_device_flags():
    args = build_parser().parse_args(
        ["stream", "--device", "Living Room", "--device", "Living Room (2)"]
    )
    assert args.devices == ["Living Room", "Living Room (2)"]


def test_cli_scan_has_no_device_requirement():
    args = build_parser().parse_args(["scan"])
    assert args.command == "scan"


# ------------------------------------------- concurrent watchdog isolation
def test_one_failing_session_does_not_block_the_other(monkeypatch):
    async def run():
        stop = asyncio.Event()
        good_streams = {"n": 0}
        conf = SimpleNamespace(name="Living Room", address="10.0.0.83")

        async def fake_scan(loop, identifier=None, timeout=5, **kw):
            if identifier == "id-bad":
                return []  # this device is never found -> endless backoff
            return [conf]

        async def fake_connect(c, loop, **kw):
            async def stream_file(reader, **kwargs):
                good_streams["n"] += 1
                await stop.wait()  # healthy stream until shutdown

            return SimpleNamespace(
                stream=SimpleNamespace(stream_file=stream_file),
                close=lambda: set(),
            )

        monkeypatch.setattr(airplay.pyatv, "scan", fake_scan)
        monkeypatch.setattr(airplay.pyatv, "connect", fake_connect)

        good_stats, bad_stats = StreamStats(), StreamStats()
        policy = RetryPolicy(initial_delay=0.01, max_delay=0.02)
        both = asyncio.gather(
            stream_forever("id-good", lambda: object(), policy, stop, good_stats),
            stream_forever("id-bad", lambda: object(), policy, stop, bad_stats),
        )
        await asyncio.sleep(0.15)  # let good stream connect, bad keep retrying
        assert good_streams["n"] == 1
        assert bad_stats.failures >= 2
        stop.set()
        await asyncio.wait_for(both, timeout=2.0)
        assert good_stats.connects == 1

    asyncio.run(run())


# ------------------------------------------------------- connection events
def test_stream_forever_emits_ordered_connection_events(monkeypatch):
    async def run():
        stop = asyncio.Event()
        events = []
        calls = {"n": 0}
        conf = SimpleNamespace(name="Living Room", address="10.0.0.83")

        async def fake_scan(loop, identifier=None, timeout=5, **kw):
            return [conf]

        async def fake_connect(c, loop, **kw):
            async def stream_file(reader, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("mid-stream drop")
                stop.set()

            return SimpleNamespace(
                stream=SimpleNamespace(stream_file=stream_file),
                close=lambda: set(),
            )

        monkeypatch.setattr(airplay.pyatv, "scan", fake_scan)
        monkeypatch.setattr(airplay.pyatv, "connect", fake_connect)

        await stream_forever(
            "id-1",
            lambda: object(),
            RetryPolicy(initial_delay=0.01, max_delay=0.02),
            stop,
            StreamStats(),
            on_event=lambda ev, payload: events.append(ev),
        )
        return events

    events = asyncio.run(run())
    assert events == [
        "connecting", "connected", "disconnected",
        "connecting", "connected", "disconnected",
    ]
