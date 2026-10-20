import asyncio
from types import SimpleNamespace

from homepod_bridge import airplay
from homepod_bridge.airplay import DeviceInfo, RetryPolicy
from pyatv.const import PairingRequirement


def devices():
    return [DeviceInfo(name, address, name, PairingRequirement.NotNeeded)
            for name, address in (("left", "10.0.0.1"), ("right", "10.0.0.2"))]


def test_group_reconnects_all_members_with_one_fresh_audio_reader(monkeypatch):
    from homepod_bridge import group_stream, native_sender

    async def run():
        stop = asyncio.Event()
        opened, closed, events, commands = [], [], [], []

        async def scan(*args, **kwargs):
            return [SimpleNamespace(identifier=d.identifier) for d in devices()]

        class Session:
            attempts = 0

            @classmethod
            async def connect(cls, command, reader, *, target_count):
                cls.attempts += 1
                assert target_count == 2
                commands.append(command)
                self = cls()
                self.reader = reader
                return self

            def target(self, index):
                return SimpleNamespace(audio=SimpleNamespace(index=index))

            async def run(self):
                if self.attempts == 1:
                    raise ConnectionError("second endpoint disconnected")
                stop.set()

            def close(self):
                async def cleanup():
                    closed.append(self.reader)
                return {asyncio.create_task(cleanup())}

        monkeypatch.setattr(airplay.pyatv, "scan", scan)
        monkeypatch.setattr(native_sender, "group_command_for", lambda confs: ["group", str(len(confs))])
        monkeypatch.setattr(native_sender, "NativeSession", Session)

        def reader(identifier):
            opened.append(identifier)
            return object()

        readers = {d.identifier: lambda ident=d.identifier: reader(ident) for d in devices()}
        callbacks = {d.identifier: lambda event, payload=None, ident=d.identifier:
                     events.append((ident, event)) for d in devices()}
        await group_stream.stream_group_forever(
            devices(), readers, callbacks, stop,
            policy=RetryPolicy(initial_delay=0.001),
        )
        assert opened == ["left", "left"], "Each group attempt must consume just one source"
        assert len(closed) == 2
        assert commands == [["group", "2"], ["group", "2"]]
        assert events.count(("left", "connected")) == 2
        assert events.count(("right", "connected")) == 2
        assert events.count(("left", "disconnected")) == 2
        assert events.count(("right", "disconnected")) == 2

    asyncio.run(run())


def test_group_does_not_start_with_only_one_discovered_member(monkeypatch):
    from homepod_bridge import group_stream, native_sender

    async def run():
        stop = asyncio.Event()
        scans = []

        async def scan(*args, **kwargs):
            scans.append(kwargs)
            if len(scans) == 2:
                stop.set()
            return [SimpleNamespace(identifier="left")]

        monkeypatch.setattr(airplay.pyatv, "scan", scan)
        monkeypatch.setattr(native_sender, "group_command_for",
                            lambda _: (_ for _ in ()).throw(AssertionError("incomplete group")))
        await group_stream.stream_group_forever(
            devices(), {}, {}, stop, policy=RetryPolicy(initial_delay=0.001),
        )
        assert len(scans) == 2

    asyncio.run(run())


def test_unsupported_group_retains_individual_device_fallback(monkeypatch):
    from homepod_bridge import group_stream, native_sender

    async def run():
        stop = asyncio.Event()
        streamed = []

        async def scan(*args, **kwargs):
            return [SimpleNamespace(identifier=d.identifier) for d in devices()]

        async def individual(identifier, reader, **kwargs):
            streamed.append((identifier, reader(), kwargs["prefer_native"]))

        monkeypatch.setattr(airplay.pyatv, "scan", scan)
        monkeypatch.setattr(native_sender, "group_command_for", lambda _: None)
        monkeypatch.setattr(airplay, "stream_forever", individual)
        await group_stream.stream_group_forever(
            devices(), {"left": lambda: 1, "right": lambda: 2},
            {"left": lambda *args: None, "right": lambda *args: None}, stop,
        )
        assert streamed == [("left", 1, False), ("right", 2, False)]

    asyncio.run(run())
