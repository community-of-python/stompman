import asyncio
import socket
import time
from collections.abc import AsyncGenerator, Generator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from ssl import SSLContext
from typing import Literal, Protocol, Self, cast

from stompman.errors import ConnectionLostError
from stompman.frames import AnyClientFrame, AnyServerFrame
from stompman.serde import NEWLINE, FrameParser, dump_frame


@dataclass(kw_only=True)
class AbstractConnection(Protocol):
    last_read_time: float | None = field(init=False, default=None)

    @classmethod
    async def connect(
        cls,
        *,
        host: str,
        port: int,
        timeout: int,
        read_max_chunk_size: int,
        ssl: Literal[True] | SSLContext | None,
    ) -> Self | None: ...
    async def close(self) -> None: ...
    def write_heartbeat(self) -> None: ...
    async def write_frame(self, frame: AnyClientFrame) -> None: ...
    def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]: ...


@contextmanager
def _reraise_connection_lost(*causes: type[Exception]) -> Generator[None, None, None]:
    try:
        yield
    except causes as exception:
        raise ConnectionLostError(reason=exception) from exception


@dataclass(kw_only=True, slots=True)
class Connection(AbstractConnection):
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    read_max_chunk_size: int
    ssl: Literal[True] | SSLContext | None

    @classmethod
    async def connect(
        cls,
        *,
        host: str,
        port: int,
        timeout: int,
        read_max_chunk_size: int,
        ssl: Literal[True] | SSLContext | None,
    ) -> Self | None:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ssl), timeout=timeout)
        except (TimeoutError, ConnectionError, socket.gaierror):
            return None
        else:
            return cls(
                reader=reader,
                writer=writer,
                read_max_chunk_size=read_max_chunk_size,
                ssl=ssl,
            )

    async def close(self) -> None:
        self.writer.close()
        with suppress(ConnectionError):
            await self.writer.wait_closed()

    def write_heartbeat(self) -> None:
        with _reraise_connection_lost(RuntimeError):
            return self.writer.write(NEWLINE)

    async def write_frame(self, frame: AnyClientFrame) -> None:
        with _reraise_connection_lost(RuntimeError):
            self.writer.write(dump_frame(frame))
        with _reraise_connection_lost(ConnectionError):
            await self.writer.drain()

    async def _read_non_empty_bytes(self, max_chunk_size: int) -> bytes:
        if (chunk := await self.reader.read(max_chunk_size)) == b"":
            raise ConnectionLostError(reason="eof")
        return chunk

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        parser = FrameParser()

        while True:
            with _reraise_connection_lost(ConnectionError):
                raw_frames = await self._read_non_empty_bytes(self.read_max_chunk_size)
            self.last_read_time = time.time()

            for frame in cast("Iterator[AnyServerFrame]", parser.parse_frames_from_chunk(raw_frames)):
                yield frame
