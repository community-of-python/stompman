"""Acquire a negotiated connection before a session can exist."""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Self, cast

from ._tasks import await_cleanup
from .config import ConnectionSettings, Heartbeat, Server
from .errors import ConnectionConfirmationTimeout, StompProtocolConnectionIssue, UnsupportedProtocolVersion
from .frames import (
    AnyServerFrame,
    ConnectedFrame,
    ConnectFrame,
    ConnectHeaders,
    ErrorFrame,
    HeartbeatFrame,
    MessageFrame,
    ReceiptFrame,
)
from .transport import Transport


@dataclass(kw_only=True)
class HandshakeFailedError(Exception):
    issue: StompProtocolConnectionIssue


async def handshake(
    transport: Transport, server: Server, settings: ConnectionSettings, frames: AsyncGenerator[AnyServerFrame, None]
) -> Heartbeat:
    headers = cast(
        "ConnectHeaders",
        dict(server.connect_headers)
        | {
            "accept-version": settings.protocol_version,
            "host": server.host,
            "login": server.login,
            "passcode": server.passcode,
            "heart-beat": settings.heartbeat.to_header(),
        },
    )
    collected: list[MessageFrame | ReceiptFrame | ErrorFrame | HeartbeatFrame] = []
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        async with asyncio.timeout(settings.handshake_timeout):
            await transport.write_frame(ConnectFrame(headers=headers))
            async for frame in frames:
                if isinstance(frame, ConnectedFrame):
                    version = frame.headers.get("version", "")
                    if version != settings.protocol_version:
                        raise HandshakeFailedError(
                            issue=UnsupportedProtocolVersion(
                                given_version=version, supported_version=settings.protocol_version
                            )
                        )
                    return settings.heartbeat.negotiate(Heartbeat.from_header(frame.headers.get("heart-beat", "0,0")))
                collected.append(frame)
                if isinstance(frame, ErrorFrame):
                    break
    except TimeoutError:
        pass
    raise HandshakeFailedError(
        issue=ConnectionConfirmationTimeout(timeout=settings.handshake_timeout, frames=collected)
    )


@dataclass(frozen=True, slots=True)
class NegotiatedConnection:
    transport: Transport
    frames: AsyncGenerator[AnyServerFrame, None]
    heartbeat: Heartbeat

    @classmethod
    async def open(cls, transport: Transport, server: Server, settings: ConnectionSettings) -> Self:
        frames = transport.read_frames()

        async def cleanup() -> None:
            try:
                await frames.aclose()
            finally:
                await transport.close()

        try:
            heartbeat = await handshake(transport, server, settings, frames)
        except BaseException:
            await await_cleanup(asyncio.create_task(cleanup()))
            raise
        return cls(transport, frames, heartbeat)

    async def close(self) -> None:
        try:
            await self.frames.aclose()
        finally:
            await self.transport.close()
