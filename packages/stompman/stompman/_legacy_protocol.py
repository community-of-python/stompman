"""Historical handshake and frame tolerance, explicitly selected by Client."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast

from .core.acknowledgement import Acknowledgement, Decision
from .core.config import Confirmation, ConnectionSettings, Heartbeat, Server
from .core.errors import ConnectionConfirmationTimeout, HandshakeFailedError, UnsupportedProtocolVersion
from .core.frames import (
    AckMode,
    AnyClientFrame,
    AnyServerFrame,
    ConnectedFrame,
    ConnectFrame,
    ConnectHeaders,
    ErrorFrame,
    HeartbeatFrame,
    MessageFrame,
    ReceiptFrame,
)
from .core.protocol import Stomp12
from .core.transport import Transport

if TYPE_CHECKING:
    from .core.session import Session


class MissingAcknowledgement:
    @staticmethod
    async def send(decision: Decision) -> None:
        del decision
        logging.getLogger("stompman").warning("failed to settle message frame: it has no ack header")


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


class LegacyProtocol(Stomp12):
    @staticmethod
    def acknowledgement(
        frame: MessageFrame,
        session: "Session",
        subscription_id: str,
        confirmation: Confirmation,
    ) -> Acknowledgement:
        if not frame.headers.get("ack"):
            return MissingAcknowledgement()
        return Stomp12.acknowledgement(frame, session, subscription_id, confirmation)

    @staticmethod
    def validate_settings(settings: ConnectionSettings) -> None:
        pass

    @staticmethod
    def outgoing(frame: AnyClientFrame) -> None:
        pass

    @staticmethod
    def incoming(frame: AnyServerFrame) -> None:
        pass

    @staticmethod
    def delivery(frame: MessageFrame, ack: AckMode) -> None:
        pass

    @staticmethod
    async def negotiate(
        transport: Transport,
        server: Server,
        settings: ConnectionSettings,
        frames: AsyncGenerator[AnyServerFrame, None],
    ) -> Heartbeat:
        return await handshake(transport, server, settings, frames)
