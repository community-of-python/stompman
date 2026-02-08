import asyncio
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import suppress
from dataclasses import dataclass
from ssl import SSLContext
from typing import Literal, Self, cast

from websockets.asyncio.client import ClientConnection as WSClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from stompman.connection import AbstractConnection, reraise_connection_lost
from stompman.frames import AnyClientFrame, AnyServerFrame
from stompman.serde import NEWLINE, FrameParser, dump_frame


@dataclass(kw_only=True)
class WebSocketConnection(AbstractConnection):
    websocket: WSClientConnection
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
        uri_path: str,
    ) -> Self | None:
        try:
            uri = f"ws://{host}:{port}/{uri_path.strip('/')}"
            websocket = await asyncio.wait_for(
                ws_connect(uri=uri, ssl=ssl, max_size=read_max_chunk_size), timeout=timeout
            )
        except (TimeoutError, WebSocketException):
            return None
        else:
            return cls(websocket=websocket, read_max_chunk_size=read_max_chunk_size, ssl=ssl)

    async def close(self) -> None:
        with suppress(WebSocketException):
            await self.websocket.close()

    def write_heartbeat(self) -> None:
        with reraise_connection_lost(RuntimeError, WebSocketException):
            asyncio.run_coroutine_threadsafe(self.websocket.send(NEWLINE, text=True), loop=asyncio.get_running_loop())

    async def write_frame(self, frame: AnyClientFrame) -> None:
        with reraise_connection_lost(RuntimeError, WebSocketException):
            await self.websocket.send(dump_frame(frame), text=True)

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        parser = FrameParser()

        while True:
            with reraise_connection_lost(WebSocketException):
                raw_frames = await self.websocket.recv(decode=False)
            self.last_read_time = time.time()

            for frame in cast("Iterator[AnyServerFrame]", parser.parse_frames_from_chunk(raw_frames)):
                yield frame
