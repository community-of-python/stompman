import asyncio
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import suppress
from dataclasses import dataclass
from ssl import SSLContext
from typing import Literal, Self, cast

import websockets
from websockets.exceptions import WebSocketException

from stompman.connection import AbstractConnection, reraise_connection_lost
from stompman.frames import AnyClientFrame, AnyServerFrame
from stompman.serde import NEWLINE, FrameParser, dump_frame


@dataclass(kw_only=True)
class WebSocketConnection(AbstractConnection):
    websocket: websockets.ClientConnection
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
        ws_uri_path: str | None,
    ) -> Self | None:
        try:
            path = f"{ws_uri_path.strip('/')}" if ws_uri_path else ""
            uri = f"ws://{host}:{port}/{path}"
            websocket = await asyncio.wait_for(
                websockets.connect(uri=uri, ssl=ssl, max_size=read_max_chunk_size), timeout=timeout
            )
        except (TimeoutError, OSError, WebSocketException):
            return None
        else:
            return cls(websocket=websocket, read_max_chunk_size=read_max_chunk_size, ssl=ssl)

    async def close(self) -> None:
        with suppress(WebSocketException):
            await self.websocket.close()

    def write_heartbeat(self) -> None:
        with reraise_connection_lost(RuntimeError, OSError, WebSocketException):
            asyncio.run_coroutine_threadsafe(self.websocket.send(NEWLINE, text=True), loop=asyncio.get_running_loop())

    async def write_frame(self, frame: AnyClientFrame) -> None:
        with reraise_connection_lost(RuntimeError, OSError, WebSocketException):
            await self.websocket.send(dump_frame(frame), text=True)

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        parser = FrameParser()

        while True:
            with reraise_connection_lost(RuntimeError, OSError, WebSocketException):
                raw_frames = await self.websocket.recv(decode=False)
            self.last_read_time = time.time()

            for frame in cast("Iterator[AnyServerFrame]", parser.parse_frames_from_chunk(raw_frames)):
                yield frame
