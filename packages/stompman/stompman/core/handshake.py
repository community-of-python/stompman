"""Acquire a negotiated connection before a session can exist."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Self

from ._tasks import await_cleanup
from .config import ConnectionSettings, Heartbeat, Server
from .errors import HandshakeFailedError
from .frames import AnyServerFrame
from .protocol import STOMP_12, Stomp12
from .transport import Transport

__all__ = ["HandshakeFailedError", "NegotiatedConnection", "handshake"]


handshake = STOMP_12.negotiate


@dataclass(frozen=True, slots=True)
class NegotiatedConnection:
    transport: Transport
    frames: AsyncGenerator[AnyServerFrame, None]
    heartbeat: Heartbeat
    protocol: Stomp12 = STOMP_12

    @classmethod
    async def open(
        cls,
        transport: Transport,
        server: Server,
        settings: ConnectionSettings,
        protocol: Stomp12 = STOMP_12,
    ) -> Self:
        frames = transport.read_frames()

        async def cleanup() -> None:
            try:
                await frames.aclose()
            finally:
                await transport.close()

        try:
            heartbeat = await protocol.negotiate(transport, server, settings, frames)
        except BaseException:
            await await_cleanup(asyncio.create_task(cleanup()))
            raise
        return cls(transport, frames, heartbeat, protocol)

    async def close(self) -> None:
        try:
            await self.frames.aclose()
        finally:
            await self.transport.close()
