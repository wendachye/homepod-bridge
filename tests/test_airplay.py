import asyncio
import itertools
from types import SimpleNamespace

import pytest
from pyatv.const import PairingRequirement

from homepod_bridge import airplay
from homepod_bridge.airplay import (
    DeviceInfo,
    RetryPolicy,
    StreamStats,
    pick_device,
    stream_forever,
)


def dev(name: str, pairing=PairingRequirement.NotNeeded) -> DeviceInfo:
    return DeviceInfo(name=name, address="10.0.0.9", identifier=f"id-{name}", raop_pairing=pairing)


# ---------------------------------------------------------------- pick_device
def test_pick_exact_match_wins_over_partial():
    devices = [dev("Bedroom"), dev("Bedroom Mini")]
    assert pick_device(devices, "bedroom").name == "Bedroom"


def test_pick_unique_partial_match():
    devices = [dev("Living Room HomePod"), dev("Office")]
    assert pick_device(devices, "living").name == "Living Room HomePod"


def test_pick_ambiguous_raises():
    with pytest.raises(LookupError, match="Ambiguous"):
        pick_device([dev("Room A"), dev("Room B")], "room")


def test_pick_no_match_lists_seen_devices():
    with pytest.raises(LookupError, match="Office"):
        pick_device([dev("Office")], "kitchen")


# ---------------------------------------------------------------- streamable
@pytest.mark.parametrize(
    "pairing,expected",
    [
        (PairingRequirement.NotNeeded, True),
        (PairingRequirement.Optional, True),
        (PairingRequirement.Disabled, False),
        (PairingRequirement.Mandatory, False),
        (PairingRequirement.Unsupported, False),
        (None, False),
    ],
)
def test_streamable_gate(pairing, expected):
    assert dev("X", pairing).streamable is expected


# ---------------------------------------------------------------- RetryPolicy
def test_retry_delays_double_and_cap():
    p = RetryPolicy(initial_delay=1, max_delay=8, factor=2)
    assert list(itertools.islice(p.delays(), 6)) == [1, 2, 4, 8, 8, 8]


# ---------------------------------------------------------------- watchdog
class FakeAtv:
    def __init__(self, stream_fn):
        self.stream = SimpleNamespace(stream_file=stream_fn)
        self.closed = False

    def close(self):
        self.closed = True
        return set()


def test_watchdog_retries_then_streams_until_stopped(monkeypatch):
    async def run() -> StreamStats:
        stop = asyncio.Event()
        stats = StreamStats()
        attempts = {"n": 0}
        conf = SimpleNamespace(name="Bedroom", address="10.0.0.9")
        atvs = []

        async def fake_scan(loop, identifier=None, timeout=5, **kw):
            assert identifier == "id-1"
            return [conf]

        async def fake_connect(c, loop, **kw):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("boom")

            async def stream_file(reader, **kwargs):
                stop.set()  # simulate a healthy stream ending at shutdown

            atv = FakeAtv(stream_file)
            atvs.append(atv)
            return atv

        monkeypatch.setattr(airplay.pyatv, "scan", fake_scan)
        monkeypatch.setattr(airplay.pyatv, "connect", fake_connect)

        readers = []
        await stream_forever(
            "id-1",
            lambda: readers.append(object()) or readers[-1],
            policy=RetryPolicy(initial_delay=0.01, max_delay=0.02),
            stop_event=stop,
            stats=stats,
        )
        assert attempts["n"] == 3
        assert len(readers) == 1  # fresh reader only for the successful connect
        assert all(a.closed for a in atvs)
        return stats

    stats = asyncio.run(run())
    assert stats.connects == 1
    assert stats.failures == 2
    assert "boom" in stats.last_error


def test_watchdog_reconnects_after_midstream_drop(monkeypatch):
    async def run():
        stop = asyncio.Event()
        stats = StreamStats()
        calls = {"n": 0}
        conf = SimpleNamespace(name="Bedroom", address="10.0.0.9")

        async def fake_scan(loop, identifier=None, timeout=5, **kw):
            return [conf]

        async def fake_connect(c, loop, **kw):
            async def stream_file(reader, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("RTSP SETUP failed")  # mid-stream drop
                stop.set()

            return FakeAtv(stream_file)

        monkeypatch.setattr(airplay.pyatv, "scan", fake_scan)
        monkeypatch.setattr(airplay.pyatv, "connect", fake_connect)

        await stream_forever(
            "id-1",
            lambda: object(),
            policy=RetryPolicy(initial_delay=0.01, max_delay=0.02),
            stop_event=stop,
            stats=stats,
        )
        return calls["n"], stats

    calls, stats = asyncio.run(run())
    assert calls == 2
    assert stats.connects == 2
    assert stats.failures == 1


def test_backoff_not_reset_by_slow_failing_connects(monkeypatch):
    """A stale mDNS record makes scan succeed but connect hang for ~20s; that
    time is NOT healthy streaming and must not reset the backoff to 1s."""

    async def run():
        stop = asyncio.Event()
        clock = {"now": 0.0}
        monkeypatch.setattr(
            airplay, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        conf = SimpleNamespace(name="Bedroom", address="10.0.0.9")

        async def fake_scan(loop, identifier=None, timeout=5, **kw):
            return [conf]

        attempts = {"n": 0}

        async def fake_connect(c, loop, **kw):
            attempts["n"] += 1
            clock["now"] += 15.0  # slower than reset_after=10
            if attempts["n"] >= 4:
                stop.set()
            raise ConnectionError("stale record")

        monkeypatch.setattr(airplay.pyatv, "scan", fake_scan)
        monkeypatch.setattr(airplay.pyatv, "connect", fake_connect)

        resets = {"n": 0}

        class CountingPolicy(RetryPolicy):
            def delays(self):
                resets["n"] += 1
                return super().delays()

        await stream_forever(
            "id-1",
            lambda: object(),
            policy=CountingPolicy(
                initial_delay=0.0, max_delay=0.0, reset_after=10.0
            ),
            stop_event=stop,
        )
        return resets["n"]

    assert asyncio.run(run()) == 1  # only the initial delays(); never reset


def test_backoff_resets_after_healthy_streaming(monkeypatch):
    async def run():
        stop = asyncio.Event()
        clock = {"now": 0.0}
        monkeypatch.setattr(
            airplay, "time", SimpleNamespace(monotonic=lambda: clock["now"])
        )
        conf = SimpleNamespace(name="Bedroom", address="10.0.0.9")

        async def fake_scan(loop, identifier=None, timeout=5, **kw):
            return [conf]

        calls = {"n": 0}

        async def fake_connect(c, loop, **kw):
            async def stream_file(reader, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    clock["now"] += 15.0  # streamed healthily > reset_after
                    raise OSError("drop after a long healthy stream")
                stop.set()

            return FakeAtv(stream_file)

        monkeypatch.setattr(airplay.pyatv, "scan", fake_scan)
        monkeypatch.setattr(airplay.pyatv, "connect", fake_connect)

        resets = {"n": 0}

        class CountingPolicy(RetryPolicy):
            def delays(self):
                resets["n"] += 1
                return super().delays()

        await stream_forever(
            "id-1",
            lambda: object(),
            policy=CountingPolicy(
                initial_delay=0.0, max_delay=0.0, reset_after=10.0
            ),
            stop_event=stop,
        )
        return resets["n"]

    assert asyncio.run(run()) == 2  # initial + one reset after healthy stream


def test_stop_during_connect_emits_no_connected_and_closes(monkeypatch):
    """Disconnect while a task is mid-connect: it must not announce a
    connection (which flipped the engine back to STREAMING mid-stop) and must
    still close the device."""

    async def run():
        stop = asyncio.Event()
        events = []
        atvs = []
        conf = SimpleNamespace(name="Bedroom", address="10.0.0.9")

        async def fake_scan(loop, identifier=None, timeout=5, **kw):
            return [conf]

        async def fake_connect(c, loop, **kw):
            stop.set()  # the user clicked Disconnect while we were connecting

            async def stream_file(reader, **kwargs):
                raise AssertionError("must not stream after stop")

            atv = FakeAtv(stream_file)
            atvs.append(atv)
            return atv

        monkeypatch.setattr(airplay.pyatv, "scan", fake_scan)
        monkeypatch.setattr(airplay.pyatv, "connect", fake_connect)

        await stream_forever(
            "id-1",
            lambda: object(),
            policy=RetryPolicy(initial_delay=0.01, max_delay=0.01),
            stop_event=stop,
            on_event=lambda e, p=None: events.append(e),
        )
        return events, atvs

    events, atvs = asyncio.run(run())
    assert "connecting" in events
    assert "connected" not in events and "disconnected" not in events
    assert atvs and all(a.closed for a in atvs)


def test_watchdog_exits_promptly_when_stopped_during_backoff(monkeypatch):
    async def run():
        stop = asyncio.Event()

        async def fake_scan(loop, identifier=None, timeout=5, **kw):
            return []  # device never found -> backoff path

        monkeypatch.setattr(airplay.pyatv, "scan", fake_scan)

        task = asyncio.create_task(
            stream_forever(
                "id-1",
                lambda: object(),
                policy=RetryPolicy(initial_delay=5.0, max_delay=5.0),
                stop_event=stop,
            )
        )
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)  # must not wait the full 5s

    asyncio.run(run())


def test_failure_disconnects_and_cleans_up_before_retry_delay(monkeypatch):
    async def run():
        stop = asyncio.Event()
        cleaned = asyncio.Event()
        events = []
        scans = []

        async def scan(*args, **kwargs):
            scans.append(True)
            return [SimpleNamespace(name="Bedroom", address="10.0.0.9")]

        async def broken_stream(reader):
            raise OSError("RTSP SETUP failed")

        class Atv(FakeAtv):
            def close(self):
                super().close()

                async def cleanup():
                    assert events[-1] == "disconnected"
                    cleaned.set()

                return {asyncio.create_task(cleanup())}

        async def connect(*args, **kwargs):
            return Atv(broken_stream)

        monkeypatch.setattr(airplay.pyatv, "scan", scan)
        monkeypatch.setattr(airplay.pyatv, "connect", connect)
        task = asyncio.create_task(stream_forever(
            "id", object, policy=RetryPolicy(initial_delay=30), stop_event=stop,
            on_event=lambda event, payload: events.append(event),
        ))
        try:
            await asyncio.wait_for(cleaned.wait(), timeout=1)
            assert not task.done()
            assert len(scans) == 1
            assert events == ["connecting", "connected", "disconnected"]
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(run())
