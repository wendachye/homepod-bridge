import asyncio
import threading

import pytest

from homepod_bridge import capture, cli
from homepod_bridge.capture import CaptureFormat
from tests.test_airplay import dev


@pytest.mark.parametrize("failure_phase", ["start", "discovery", "stream"])
def test_capture_failure_exits_even_before_discovery_finishes(monkeypatch, capsys, failure_phase):
    captures = []

    class Capture:
        fmt = CaptureFormat(48000, 2)

        def __init__(self, sink, on_failure):
            self.on_failure = on_failure
            self.stopped = False
            captures.append(self)

        def fail(self):
            thread = threading.Thread(target=self.on_failure)
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive()

        def start(self):
            if failure_phase == "start":
                self.fail()

        def stop(self):
            self.stopped = True

    async def scan(timeout):
        if failure_phase == "discovery":
            captures[0].fail()
        await asyncio.sleep(0)
        return [dev("Bedroom")]

    async def stream(identifier, open_reader, policy, stop_event, stats, prefer_native):
        assert prefer_native
        assert failure_phase == "stream", "must not stream with already-dead capture"
        reader = open_reader()
        assert reader.read(44).startswith(b"RIFF")
        captures[0].fail()
        await asyncio.wait_for(stop_event.wait(), timeout=1)
        assert await asyncio.wait_for(asyncio.to_thread(reader.read), timeout=1) == b""
        reader.close()

    monkeypatch.setattr(capture, "LoopbackCapture", Capture)
    monkeypatch.setattr(cli, "scan_devices", scan)
    monkeypatch.setattr(cli, "stream_forever", stream)
    args = cli.build_parser().parse_args(["stream", "--device", "Bedroom"])
    assert cli.cmd_stream(args) == 1
    assert captures[0].stopped
    assert "Audio capture died" in capsys.readouterr().out


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_capture_start_is_cleaned_up(monkeypatch, capsys, cleanup_fails):
    stopped = []

    class Capture:
        fmt = CaptureFormat(48000, 2)

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise OSError("output unavailable")

        def stop(self):
            stopped.append(True)
            if cleanup_fails:
                raise OSError("cleanup also failed")

    monkeypatch.setattr(capture, "LoopbackCapture", Capture)
    args = cli.build_parser().parse_args(["stream", "--device", "Bedroom"])
    assert cli.cmd_stream(args) == 2
    assert stopped == [True]
    assert "output unavailable" in capsys.readouterr().out


def test_capture_construction_error_is_reported(monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise OSError("no audio endpoint")

    monkeypatch.setattr(capture, "LoopbackCapture", unavailable)
    args = cli.build_parser().parse_args(["stream", "--device", "Bedroom"])
    assert cli.cmd_stream(args) == 2
    assert "no audio endpoint" in capsys.readouterr().out
