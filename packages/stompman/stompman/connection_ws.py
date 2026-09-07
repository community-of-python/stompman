import asyncio
import time
from collections import deque
from collections.abc import AsyncGenerator, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from ssl import SSLContext, create_default_context
from typing import Literal, Self, cast

import websockets  # type: ignore[import-not-found,unused-ignore]
from websockets.asyncio.client import ClientConnection  # type: ignore[import-not-found,unused-ignore]

from stompman.connection import AbstractConnection, reraise_connection_lost
from stompman.frames import AnyClientFrame, AnyServerFrame
from stompman.serde import NEWLINE, FrameParser, dump_frame


@dataclass(kw_only=True)
class WebSocketConnection(AbstractConnection):
    websocket: ClientConnection
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
        timeout: int,
        read_max_chunk_size: int,
        ssl: Literal[True] | SSLContext | None,
        ws_uri_path: str | None = None,
    ) -> Self | None:
        try:
            path = f"{ws_uri_path.strip('/')}" if ws_uri_path else ""
            scheme = "ws" if ssl is None else "wss"
            uri = f"{scheme}://{host}:{port}/{path}"
            ssl_context = create_default_context() if ssl is True else ssl
            websocket = await asyncio.wait_for(
                websockets.connect(uri=uri, ssl=ssl_context, max_size=read_max_chunk_size), timeout=timeout
            )
        except (TimeoutError, OSError, websockets.WebSocketException):
            return None
        else:
            return cls(websocket=websocket, read_max_chunk_size=read_max_chunk_size, ssl=ssl)

    async def close(self) -> None:
        with suppress(websockets.WebSocketException):
            await self.websocket.close()

    def write_heartbeat(self) -> None:
        with reraise_connection_lost(RuntimeError, OSError, websockets.WebSocketException):
            asyncio.run_coroutine_threadsafe(self.websocket.send(NEWLINE, text=True), loop=asyncio.get_running_loop())

    async def send_heartbeat(self) -> None:
        with reraise_connection_lost(RuntimeError, OSError, websockets.WebSocketException):
            await self.websocket.send(NEWLINE, text=True)

    async def write_frame(self, frame: AnyClientFrame) -> None:
        with reraise_connection_lost(RuntimeError, OSError, websockets.WebSocketException):
            await self.websocket.send(dump_frame(frame), text=True)

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        while True:
            if not self._pending_frames:
                with reraise_connection_lost(RuntimeError, OSError, websockets.WebSocketException):
                    raw_frames = await self.websocket.recv(decode=False)
                self.last_read_time = time.time()
                self._pending_frames.extend(
                    cast("Iterator[AnyServerFrame]", self._parser.parse_frames_from_chunk(raw_frames))
                )
            while self._pending_frames:
                yield self._pending_frames.popleft()
