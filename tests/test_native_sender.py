import asyncio
import io
import sys

import pytest

from homepod_bridge import native_sender
from homepod_bridge.pcm_pipe import wav_header


HELPER = r'''
import json, struct, sys
print(json.dumps({'event': 'ready', 'protocol': 1}), flush=True)
while True:
    header = sys.stdin.buffer.read(5)
    if not header:
        break
    kind, size = struct.unpack('<BI', header)
    body = sys.stdin.buffer.read(size)
    if kind == 1:
        assert len(body) % 4 == 0
    elif kind == 2:
        assert struct.unpack('<f', body)[0] == 0.25
        print(json.dumps({'event': 'volume', 'value': 0.25}), flush=True)
    elif kind == 3:
        break
'''


def test_live_pcm_volume_and_clean_exit(tmp_path):
    helper = tmp_path / 'helper.py'
    helper.write_text(HELPER)

    async def run():
        reader = io.BytesIO(wav_header(48000, 2) + bytes(4800 * 4))
        session = await native_sender.NativeSession.connect(
            [sys.executable, '-u', str(helper)], reader,
        )
        await session.audio.set_volume(25)
        await session.run()
        await asyncio.gather(*session.close())
        assert reader.closed
        assert session.process.returncode == 0

    asyncio.run(run())


def test_failure_before_ready_closes_reader(tmp_path):
    helper = tmp_path / 'helper.py'
    helper.write_text("import sys; sys.exit(7)")

    async def run():
        reader = io.BytesIO(wav_header(44100, 2))
        with pytest.raises(ConnectionError, match='before ready'):
            await native_sender.NativeSession.connect(
                [sys.executable, str(helper)], reader,
            )
        assert reader.closed

    asyncio.run(run())


def test_invalid_pcm_rejected_before_launch():
    async def run():
        reader = io.BytesIO(wav_header(48000, 2, sample_width=4))
        with pytest.raises(ValueError, match='16-bit'):
            await native_sender.NativeSession.connect(['must-not-run'], reader)
        assert reader.closed

    asyncio.run(run())


def test_disconnect_unblocks_live_read_and_reaps_child(tmp_path):
    from homepod_bridge.pcm_pipe import PcmPipe

    helper = tmp_path / 'helper.py'
    helper.write_text(HELPER)

    async def run():
        pipe = PcmPipe.live(48000, 2, prime_seconds=0)
        reader = pipe.reader()
        session = await native_sender.NativeSession.connect(
            [sys.executable, '-u', str(helper)], reader,
        )
        task = asyncio.create_task(session.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(asyncio.gather(*session.close()), 3)
        assert reader.closed
        assert session.process.returncode == 0

    asyncio.run(run())


def test_child_crash_interrupts_waiting_for_capture(tmp_path):
    from homepod_bridge.pcm_pipe import PcmPipe

    helper = tmp_path / 'helper.py'
    helper.write_text(HELPER)

    async def run():
        pipe = PcmPipe.live(48000, 2, prime_seconds=0)
        session = await native_sender.NativeSession.connect(
            [sys.executable, '-u', str(helper)], pipe.reader(),
        )
        task = asyncio.create_task(session.run())
        await asyncio.sleep(0.05)
        session.process.kill()
        with pytest.raises(ConnectionError, match='stopped unexpectedly'):
            await asyncio.wait_for(task, timeout=2)
        await asyncio.wait_for(asyncio.gather(*session.close()), timeout=2)
        assert session.reader.closed

    asyncio.run(run())


def test_only_homepods_with_helper_use_native_sender(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from pyatv.const import DeviceModel

    conf = SimpleNamespace(
        device_info=SimpleNamespace(model=DeviceModel.HomePod),
        address='10.0.0.1',
        get_service=lambda _: SimpleNamespace(port=7000, properties={'features': '0x123'}),
    )
    helper = tmp_path / 'HomePodSender.exe'
    monkeypatch.setattr(native_sender, 'helper_path', lambda: helper)
    assert native_sender.command_for(conf) == [str(helper), '10.0.0.1', '7000', '0x123']
    conf.device_info.model = DeviceModel.AppleTV4KGen2
    assert native_sender.command_for(conf) is None
    conf.device_info.model = DeviceModel.HomePod
    monkeypatch.setattr(native_sender, 'helper_path', lambda: None)
    assert native_sender.command_for(conf) is None


def test_group_helper_receives_both_targets_and_distinct_volumes(tmp_path):
    helper = tmp_path / "group_helper.py"
    helper.write_text(r'''
import json, struct, sys
print(json.dumps({'event':'ready','protocol':1,'targets':2}),flush=True)
volumes = {}
pcm = 0
while True:
    header = sys.stdin.buffer.read(5)
    if not header:
        break
    kind, size = struct.unpack('<BI', header)
    body = sys.stdin.buffer.read(size)
    if kind == 4:
        index, value = struct.unpack('<Hf', body)
        volumes[index] = value
    elif kind == 1:
        pcm += len(body)
    elif kind == 3:
        break
assert volumes == {0:0.25, 1:0.75}, volumes
assert pcm == 19200, pcm
''')

    async def run():
        session = await native_sender.NativeSession.connect(
            [sys.executable, "-u", str(helper)],
            io.BytesIO(wav_header(48000, 2) + bytes(19200)), target_count=2,
        )
        await session.target(0).audio.set_volume(25)
        await session.target(1).audio.set_volume(75)
        await session.run()
        await asyncio.gather(*session.close())
        assert session.process.returncode == 0

    asyncio.run(run())


def test_group_command_keeps_selection_order_and_rejects_duplicate_endpoints(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from pyatv.const import DeviceModel

    def config(address, model=DeviceModel.HomePodGen2):
        return SimpleNamespace(
            address=address, device_info=SimpleNamespace(model=model),
            get_service=lambda _: SimpleNamespace(port=7000, properties={"features": "0x123"}),
        )
    helper = tmp_path / "HomePodSender.exe"
    monkeypatch.setattr(native_sender, "helper_path", lambda: helper)
    left, right = config("10.0.0.1"), config("10.0.0.2")
    command = native_sender.group_command_for([right, left])
    assert command[:2] == [str(helper), "--group"]
    assert [item["host"] for item in json.loads(command[2])] == ["10.0.0.2", "10.0.0.1"]
    with pytest.raises(ValueError, match="duplicate"):
        native_sender.group_command_for([left, left])
    assert native_sender.group_command_for([left, config("10.0.0.3", DeviceModel.AppleTV4KGen2)]) is None


def test_group_rejects_partial_ready_and_closes_reader(tmp_path):
    helper = tmp_path / "partial_helper.py"
    helper.write_text(
        "import json; print(json.dumps({'event':'ready','protocol':1,'targets':1}),flush=True)"
    )

    async def run():
        reader = io.BytesIO(wav_header(48000, 2))
        with pytest.raises(ConnectionError, match="every group member"):
            await native_sender.NativeSession.connect(
                [sys.executable, str(helper)], reader, target_count=2,
            )
        assert reader.closed

    asyncio.run(run())
