"""Live PCM transport to the separately licensed AirPlay 2 sender process.

Protocol 1: stdin packets are <kind:u8, size:u32-le, payload>. Kinds are
1 (interleaved signed 16-bit PCM), 2 (0..1 float32 volume), and 3 (stop).
stdout carries JSON events; stderr carries diagnostics. No shell is used.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import struct
import subprocess
import sys

from pyatv.const import Protocol

logger = logging.getLogger(__name__)


def helper_path() -> Path | None:
    if sys.platform != "win32":
        return None
    if getattr(sys, "frozen", False):
        path = Path(sys._MEIPASS) / "native" / "HomePodSender.exe"
    else:
        path = Path(__file__).resolve().parent.parent / "dist" / "native" / "HomePodSender.exe"
    return path if path.is_file() else None


def command_for(conf) -> list[str] | None:
    """Use native PTP only for a HomePod with an advertised AirPlay service."""
    info = getattr(conf, "device_info", None)
    if "HomePod" not in str(getattr(info, "model", "")):
        return None
    helper = helper_path()
    service = conf.get_service(Protocol.AirPlay)
    if helper is None or service is None:
        return None
    features = service.properties.get("features")
    if not features:
        return None
    return [str(helper), str(conf.address), str(service.port), features]


class NativeSession:
    def __init__(self, reader):
        self.reader = reader
        self.process = None
        self.audio = self
        self._write_lock = asyncio.Lock()
        self._tasks = []
        self._closing = None
        self._stderr = None

    @classmethod
    async def connect(cls, command: list[str], reader) -> "NativeSession":
        session = cls(reader)
        try:
            header = await asyncio.to_thread(reader.read, 44)
            if (len(header) != 44 or header[:4] != b"RIFF"
                    or header[8:16] != b"WAVEfmt " or header[36:40] != b"data"):
                raise ValueError("native sender requires a PCM WAV stream")
            fmt, channels, rate, _, align, bits = struct.unpack_from("<HHIIHH", header, 20)
            if fmt != 1 or bits != 16 or channels not in (1, 2) or align != channels * 2:
                raise ValueError("native sender requires 16-bit mono or stereo PCM")
            if not 8000 <= rate <= 192000:
                raise ValueError("unsupported PCM sample rate")
            session.chunk_size = (rate // 100) * align  # 10ms, whole frames
            session.process = await asyncio.create_subprocess_exec(
                *command, str(rate), str(channels),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            session._stderr = asyncio.create_task(session._log_stderr())
            line = await asyncio.wait_for(session.process.stdout.readline(), timeout=30)
            if not line:
                raise ConnectionError("native sender exited before ready")
            event = json.loads(line)
            if event.get("event") != "ready" or event.get("protocol") != 1:
                raise ConnectionError("native sender failed before ready: " + str(event))
            logger.info("Native AirPlay 2 stereo sender ready")
            return session
        except BaseException:
            await session._close()
            raise

    async def _send(self, kind: int, payload: bytes = b"") -> None:
        async with self._write_lock:
            self.process.stdin.write(struct.pack("<BI", kind, len(payload)) + payload)
            await self.process.stdin.drain()

    async def set_volume(self, level: float) -> None:
        await self._send(2, struct.pack("<f", max(0.0, min(100.0, level)) / 100))

    async def _pump_audio(self) -> None:
        read = getattr(self.reader, "read1", self.reader.read)
        while True:
            chunk = await asyncio.to_thread(read, self.chunk_size)
            if not chunk:
                await self._send(3)
                return
            await self._send(1, chunk)

    async def _read_events(self) -> None:
        while line := await self.process.stdout.readline():
            event = json.loads(line)
            if event.get("event") == "error":
                raise ConnectionError(str(event.get("message", "native sender failed")))
            if event.get("event") == "stats":
                logger.debug("Native sender stats: %s", event)

    async def _log_stderr(self) -> None:
        while line := await self.process.stderr.readline():
            logger.warning("Native sender: %s", line.decode("utf-8", errors="replace").strip())

    async def run(self) -> None:
        pump = asyncio.create_task(self._pump_audio())
        events = asyncio.create_task(self._read_events())
        exited = asyncio.create_task(self.process.wait())
        self._tasks = [pump, events, exited]
        done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if pump in done:
            await asyncio.wait_for(exited, timeout=5)
            await events
            if self.process.returncode:
                raise ConnectionError(f"native sender exited with code {self.process.returncode}")
        else:
            raise ConnectionError("native sender stopped unexpectedly")

    def close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        return {self._closing}

    async def _close(self) -> None:
        # Closing RawIOBase wakes a blocking StreamBuffer read; closing only
        # BufferedReader could deadlock on its lock held by that read.
        raw = getattr(self.reader, "raw", None)
        if raw is not None:
            raw.close()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.to_thread(self.reader.close)
        if self.process is not None:
            if self.process.returncode is None:
                try:
                    await asyncio.wait_for(self._send(3), timeout=0.25)
                    self.process.stdin.close()
                    await asyncio.wait_for(self.process.wait(), timeout=1.5)
                except (OSError, asyncio.TimeoutError):
                    if self.process.returncode is None:
                        self.process.kill()
                    await self.process.wait()
            if self.process.stdin is not None:
                self.process.stdin.close()
        if self._stderr is not None:
            await self._stderr
