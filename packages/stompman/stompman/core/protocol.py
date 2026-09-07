"""STOMP 1.2 negotiation and frame rules, selected once for each connection."""

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, cast

from .acknowledgement import Acknowledgement, MessageAcknowledgement
from .config import Confirmation, ConnectionSettings, Heartbeat, Server
from .errors import (
    ConnectionConfirmationTimeout,
    ConnectionLostError,
    HandshakeDisconnected,
    HandshakeFailedError,
    HandshakeRejected,
    MalformedHandshake,
    ProtocolError,
    UnsupportedProtocolVersion,
)
from .frames import (
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
from .validation import require_header, validate_frame, validate_outgoing

if TYPE_CHECKING:
    from .session import Session
    from .transport import Transport


class Stomp12:
    @staticmethod
    def acknowledgement(
        frame: MessageFrame,
        session: "Session",
        subscription_id: str,
        confirmation: Confirmation,
    ) -> Acknowledgement:
        return MessageAcknowledgement(
            id=require_header(frame.headers, "ack"),
            subscription_id=subscription_id,
            session=session,
            confirmation=confirmation,
        )

    @staticmethod
    def validate_settings(settings: ConnectionSettings) -> None:
        if settings.protocol_version != "1.2":
            raise ProtocolError(reason="native sessions implement STOMP 1.2 only")

    @staticmethod
    def outgoing(frame: AnyClientFrame) -> None:
        validate_outgoing(frame)

    @staticmethod
    def incoming(frame: AnyServerFrame) -> None:
        if isinstance(frame, HeartbeatFrame):
            return
        if not isinstance(frame, (MessageFrame, ReceiptFrame, ErrorFrame)):
            raise ProtocolError(reason="unexpected command after CONNECTED")
        validate_frame(frame)

    @staticmethod
    def delivery(frame: MessageFrame, ack: AckMode) -> None:
        if ack != "auto":
            require_header(frame.headers, "ack")

    @staticmethod
    def connect_frame(server: Server, settings: ConnectionSettings) -> ConnectFrame:
        return ConnectFrame(
            headers=cast(
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
        )

    @staticmethod
    def connected(frame: ConnectedFrame, settings: ConnectionSettings) -> Heartbeat:
        validate_frame(frame)
        version = frame.headers["version"]
        if version != settings.protocol_version:
            raise HandshakeFailedError(
                issue=UnsupportedProtocolVersion(
                    given_version=version,
                    supported_version=settings.protocol_version,
                )
            )
        header = frame.headers.get("heart-beat", "0,0")
        send, separator, receive = header.partition(",")
        if not separator or any(not part.isascii() or not part.isdecimal() for part in (send, receive)):
            raise ProtocolError(reason="heart-beat requires two nonnegative decimal intervals")
        try:
            return settings.heartbeat.negotiate(Heartbeat.from_header(header))
        except ValueError as error:
            raise ProtocolError(reason="invalid heart-beat intervals") from error

    async def negotiate(
        self,
        transport: "Transport",
        server: Server,
        settings: ConnectionSettings,
        frames: AsyncGenerator[AnyServerFrame, None],
    ) -> Heartbeat:
        self.validate_settings(settings)
        connect = self.connect_frame(server, settings)
        self.outgoing(connect)
        try:
            async with asyncio.timeout(settings.handshake_timeout):
                await transport.write_frame(connect)
                return await self._response(frames, settings)
        except TimeoutError as error:
            raise HandshakeFailedError(
                issue=ConnectionConfirmationTimeout(
                    timeout=settings.handshake_timeout,
                    frames=[],
                )
            ) from error
        except ProtocolError as error:
            raise HandshakeFailedError(issue=MalformedHandshake(error=error)) from error
        except (ConnectionLostError, OSError) as error:
            failure = error if isinstance(error, ConnectionLostError) else ConnectionLostError(reason=error)
            raise HandshakeFailedError(issue=HandshakeDisconnected(error=failure)) from error

    async def _response(
        self,
        frames: AsyncGenerator[AnyServerFrame, None],
        settings: ConnectionSettings,
    ) -> Heartbeat:
        async for frame in frames:
            if isinstance(frame, HeartbeatFrame):
                continue
            if isinstance(frame, ErrorFrame):
                validate_frame(frame)
                raise HandshakeFailedError(issue=HandshakeRejected(frame=frame))
            if isinstance(frame, ConnectedFrame):
                return self.connected(frame, settings)
            raise ProtocolError(reason="expected CONNECTED or ERROR during handshake")
        raise ConnectionLostError(reason="eof during handshake")


STOMP_12 = Stomp12()
