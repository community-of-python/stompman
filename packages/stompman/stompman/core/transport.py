"""The transport seam: TCP and adapter-provided transports obey the same contract."""

import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator, Iterator
from contextlib import suppress
from typing import Protocol, cast

from .config import ConnectionSettings, Server
from .errors import ConnectionLostError
from .frames import AnyClientFrame, AnyServerFrame
from .serde import NEWLINE, FrameParser, dump_frame


class Transport(Protocol):
    @property
    def last_received_at(self) -> float:
        """Monotonic time of the last bytes received, including partial frames."""
        ...

    async def write_frame(self, frame: AnyClientFrame) -> None: ...
    async def send_heartbeat(self) -> None: ...
    def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]: ...
    async def close(self) -> None: ...


class TransportFactory(Protocol):
    async def __call__(self, server: Server, settings: ConnectionSettings) -> Transport: ...


class TcpTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, chunk_size: int) -> None:
        self._reader = reader
        self._writer = writer
        self._chunk_size = chunk_size
        self._parser = FrameParser()
        self._pending: deque[AnyServerFrame] = deque()
        self.last_received_at = time.monotonic()

    async def _send(self, data: bytes) -> None:
        try:
            self._writer.write(data)
            await self._writer.drain()
        except (RuntimeError, OSError) as error:
            raise ConnectionLostError(reason=error) from error

    async def write_frame(self, frame: AnyClientFrame) -> None:
        await self._send(dump_frame(frame))

    async def send_heartbeat(self) -> None:
        await self._send(NEWLINE)

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        while True:
            if not self._pending:
                try:
                    chunk = await self._reader.read(self._chunk_size)
                except OSError as error:
                    raise ConnectionLostError(reason=error) from error
                if not chunk:
                    raise ConnectionLostError(reason="eof")
                self.last_received_at = time.monotonic()
                self._pending.extend(cast("Iterator[AnyServerFrame]", self._parser.parse_frames_from_chunk(chunk)))
            while self._pending:
                yield self._pending.popleft()

    async def close(self) -> None:
        self._writer.close()
        with suppress(OSError):
            await self._writer.wait_closed()


async def connect_tcp(server: Server, settings: ConnectionSettings) -> Transport:
    async with asyncio.timeout(settings.timeout):
        reader, writer = await asyncio.open_connection(server.host, server.port, ssl=settings.tls or None)
    return TcpTransport(reader, writer, settings.read_chunk_size)
