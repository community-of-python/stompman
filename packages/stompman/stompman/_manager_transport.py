"""Transport adapters for the legacy manager's reader and lifespan hooks."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from typing import TYPE_CHECKING

from ._compat import LegacyTransport
from .config import Heartbeat
from .connection_lifespan import AbstractConnectionLifespan, EstablishedConnectionResult
from .core._tasks import await_cleanup
from .core.handshake import HandshakeFailedError
from .core.transport import Transport
from .errors import ConnectionLostError, StompProtocolConnectionIssue
from .frames import AnyClientFrame, AnyServerFrame, ConnectedFrame, ConnectFrame, DisconnectFrame

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager


class LifespanTransport:
    """Let an explicitly supplied lifespan own handshake and graceful disconnect."""

    def __init__(
        self,
        transport: LegacyTransport,
        lifespan: AbstractConnectionLifespan,
        result: EstablishedConnectionResult | StompProtocolConnectionIssue | ConnectionLostError,
        protocol_version: str,
    ) -> None:
        self._transport = transport
        self._lifespan = lifespan
        self._result = result
        self._protocol_version = protocol_version
        self._graceful = False

    @property
    def last_received_at(self) -> float:
        return self._transport.last_received_at

    async def write_frame(self, frame: AnyClientFrame) -> None:
        if isinstance(frame, ConnectFrame):
            if isinstance(self._result, ConnectionLostError):
                raise self._result
            if not isinstance(self._result, EstablishedConnectionResult):
                raise HandshakeFailedError(issue=self._result)
        elif isinstance(frame, DisconnectFrame):
            self._graceful = True
        else:
            await self._transport.write_frame(frame)

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        if not isinstance(self._result, EstablishedConnectionResult):
            return
        yield ConnectedFrame(
            headers={
                "version": self._protocol_version,
                "heart-beat": self._result.server_heartbeat.to_header(),
            }
        )
        async for frame in self._transport.read_frames():
            yield frame

    async def send_heartbeat(self) -> None:
        await self._transport.send_heartbeat()

    async def close(self) -> None:
        try:
            if self._graceful:
                with suppress(ConnectionLostError):
                    await self._lifespan.exit()
        finally:
            await self._transport.close()


class ObservedTransport:
    """Mirror frames for an opted-in raw reader without creating a second socket reader."""

    def __init__(
        self,
        transport: Transport,
        raw: LegacyTransport,
        lifespan: AbstractConnectionLifespan,
        manager: "ConnectionManager",
    ) -> None:
        self._transport = transport
        self.raw = raw
        self.lifespan = lifespan
        self._manager = manager
        self._graceful = False
        self._failure: asyncio.Future[ConnectionLostError] = asyncio.get_running_loop().create_future()
        self._closed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    @property
    def failed(self) -> bool:
        return self._failure.done()

    def fail(self, error: ConnectionLostError) -> None:
        """Invalidate through the native reader, outside the restoration task."""
        if not self._failure.done():
            self._failure.set_result(error)

    async def wait_closed(self) -> None:
        await asyncio.shield(self._closed)

    @property
    def last_received_at(self) -> float:
        return self._transport.last_received_at

    async def write_frame(self, frame: AnyClientFrame) -> None:
        await self._transport.write_frame(frame)
        if isinstance(frame, DisconnectFrame):
            self._graceful = True

    async def _next_frame(self, frames: AsyncGenerator[AnyServerFrame, None]) -> AnyServerFrame:
        reading = asyncio.create_task(anext(frames))

        async def finish_read() -> None:
            if not reading.done():
                reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)

        try:
            await asyncio.wait((reading, self._failure), return_when=asyncio.FIRST_COMPLETED)
            if self._failure.done():
                raise self._failure.result()
            return reading.result()
        finally:
            await await_cleanup(asyncio.create_task(finish_read()))

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        frames = self._transport.read_frames()
        try:
            while True:
                try:
                    frame = await self._next_frame(frames)
                except StopAsyncIteration:
                    return
                if isinstance(frame, ConnectedFrame):
                    yield frame
                    # Resumption after CONNECTED means this candidate became a Session.
                    self._manager._connection_opened(
                        self, Heartbeat.from_header(frame.headers.get("heart-beat", "0,0"))
                    )
                else:
                    self._manager._frame_received(self, frame)
                    yield frame
        finally:
            await frames.aclose()

    async def send_heartbeat(self) -> None:
        await self._transport.send_heartbeat()

    async def close(self) -> None:
        try:
            await self._manager._connection_closed(self, graceful=self._graceful)
        finally:
            try:
                await self._transport.close()
            finally:
                if not self._closed.done():
                    self._closed.set_result(None)
