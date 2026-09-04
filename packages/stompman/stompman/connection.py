import asyncio
import time
from collections import deque
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
        timeout: float,
        read_max_chunk_size: int,
        ssl: Literal[True] | SSLContext | None,
        ws_uri_path: str | None = None,
    ) -> Self | None: ...
    async def close(self) -> None: ...
    def write_heartbeat(self) -> None: ...
    async def write_frame(self, frame: AnyClientFrame) -> None: ...
    def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]: ...


@contextmanager
def reraise_connection_lost(*causes: type[Exception]) -> Generator[None, None, None]:
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
    _parser: FrameParser = field(default_factory=FrameParser, init=False, repr=False, compare=False)
    _pending_frames: deque[AnyServerFrame] = field(default_factory=deque, init=False, repr=False, compare=False)

    @classmethod
    async def connect(
        cls,
        *,
        host: str,
        port: int,
        timeout: float,
        read_max_chunk_size: int,
        ssl: Literal[True] | SSLContext | None,
        ws_uri_path: str | None = None,
    ) -> Self | None:
        try:
            if ws_uri_path:
                msg = "only stompman.connection_ws.WebSocketConnection supports ws_uri_path argument"
                raise AssertionError(msg)
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ssl), timeout=timeout)
        except OSError:
            return None
        else:
            return cls(
                reader=reader,
                writer=writer,
                read_max_chunk_size=read_max_chunk_size,
                ssl=ssl,
            )

    async def close(self) -> None:
        with suppress(OSError):
            self.writer.close()
            await self.writer.wait_closed()

    def write_heartbeat(self) -> None:
        with reraise_connection_lost(RuntimeError, OSError):
            return self.writer.write(NEWLINE)

    async def send_heartbeat(self) -> None:
        self.write_heartbeat()
        with reraise_connection_lost(OSError):
            await self.writer.drain()

    async def write_frame(self, frame: AnyClientFrame) -> None:
        with reraise_connection_lost(RuntimeError, OSError):
            self.writer.write(dump_frame(frame))
        with reraise_connection_lost(OSError):
            await self.writer.drain()

    async def _read_non_empty_bytes(self, max_chunk_size: int) -> bytes:
        if (chunk := await self.reader.read(max_chunk_size)) == b"":
            raise ConnectionLostError(reason="eof")
        return chunk

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        while True:
            if not self._pending_frames:
                with reraise_connection_lost(OSError):
                    raw_frames = await self._read_non_empty_bytes(self.read_max_chunk_size)
                self.last_read_time = time.time()
                self._pending_frames.extend(
                    cast("Iterator[AnyServerFrame]", self._parser.parse_frames_from_chunk(raw_frames))
                )
            while self._pending_frames:
                yield self._pending_frames.popleft()
