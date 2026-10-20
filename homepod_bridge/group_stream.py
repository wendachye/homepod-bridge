"""One live source and native clock for a group of selected HomePods."""
from __future__ import annotations

import asyncio
import logging
import time

from . import airplay, native_sender
from .airplay import RetryPolicy

logger = logging.getLogger(__name__)


async def stream_group_forever(
    targets, open_readers, on_events, stop_event, *, policy=RetryPolicy(),
):
    """Reconnect the entire group together; never reopen independent audio cursors.

    Non-HomePod selections and systems without the native helper retain the
    legacy RAOP path. No synchronized-playback guarantee is made for that path.
    """
    identifiers = [target.identifier for target in targets]
    loop = asyncio.get_running_loop()
    delays = policy.delays()

    def emit(identifier, event, payload=None):
        callback = on_events.get(identifier)
        if callback is not None:
            try:
                callback(event, payload)
            except Exception:
                logger.exception("group event callback failed")

    while not stop_event.is_set():
        session = None
        announced = []
        streaming_started = None
        delay = None
        try:
            # Directed discovery avoids losing a selected peer to a missed
            # multicast reply. Fall back to identifiers if DHCP moved a device.
            confs = await airplay.pyatv.scan(
                loop, hosts=[target.address for target in targets], timeout=5,
            )
            found = {str(conf.identifier): conf for conf in confs}
            if stop_event.is_set():
                return
            if any(identifier not in found for identifier in identifiers):
                confs = await airplay.pyatv.scan(loop, identifier=set(identifiers), timeout=5)
                found = {str(conf.identifier): conf for conf in confs}
            if stop_event.is_set():
                return
            if any(identifier not in found for identifier in identifiers):
                raise ConnectionError("waiting for every selected HomePod in the group")
            command = native_sender.group_command_for([found[i] for i in identifiers])
            if command is None:
                logger.warning(
                    "Shared-clock playback unavailable for this selection; using independent RAOP streams"
                )
                await asyncio.gather(*(
                    airplay.stream_forever(
                        i, open_readers[i], stop_event=stop_event,
                        on_event=on_events[i], prefer_native=False, policy=policy,
                    ) for i in identifiers
                ))
                return

            # All receivers consume this ONE reader. Opening one reader per
            # connection would reintroduce different decoder/queue positions.
            session = await native_sender.NativeSession.connect(
                command, open_readers[identifiers[0]](), target_count=len(identifiers),
            )
            if stop_event.is_set():
                return
            for index, identifier in enumerate(identifiers):
                announced.append(identifier)
                emit(identifier, "connected", session.target(index))
            streaming_started = time.monotonic()
            logger.info("Shared-clock AirPlay group connected: %d HomePods", len(identifiers))
            await session.run()
            if stop_event.is_set():
                return
            raise ConnectionError("shared-clock sender stopped unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if stop_event.is_set():
                return
            if streaming_started is not None and time.monotonic() - streaming_started >= policy.reset_after:
                delays = policy.delays()
            delay = next(delays)
            logger.warning("%s - reconnecting HomePod group in %.1fs", error, delay)
        finally:
            for identifier in announced:
                emit(identifier, "disconnected")
            if session is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*session.close(), return_exceptions=True), timeout=3,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("group teardown incomplete", exc_info=True)
        if delay is not None and not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
