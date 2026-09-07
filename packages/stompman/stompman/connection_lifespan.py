"""Compatibility handshake utility for users of raw Connection objects."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from .config import ConnectionParameters, Heartbeat
from .connection import AbstractConnection
from .errors import ConnectionConfirmationTimeout, StompProtocolConnectionIssue, UnsupportedProtocolVersion
from .frames import (
    ConnectedFrame,
    ConnectFrame,
    ConnectHeaders,
    DisconnectFrame,
    ErrorFrame,
    HeartbeatFrame,
    MessageFrame,
    ReceiptFrame,
)

if TYPE_CHECKING:
    from .subscription import ActiveSubscriptions


@dataclass(frozen=True, kw_only=True, slots=True)
class EstablishedConnectionResult:
    server_heartbeat: Heartbeat


class AbstractConnectionLifespan(Protocol):
    connection_parameters: ConnectionParameters

    async def enter(self) -> EstablishedConnectionResult | StompProtocolConnectionIssue: ...
    async def exit(self) -> None: ...


@dataclass(kw_only=True, slots=True)
class ConnectionLifespan:
    connection: AbstractConnection
    connection_parameters: ConnectionParameters
    protocol_version: str
    client_heartbeat: Heartbeat
    connection_confirmation_timeout: int
    disconnect_confirmation_timeout: int
    active_subscriptions: "ActiveSubscriptions"
    set_heartbeat_interval: Callable[[Heartbeat], Any]

    async def enter(self) -> EstablishedConnectionResult | StompProtocolConnectionIssue:
        headers = cast(
            "ConnectHeaders",
            self.connection_parameters.connect_headers
            | {
                "accept-version": self.protocol_version,
                "heart-beat": self.client_heartbeat.to_header(),
                "host": self.connection_parameters.host,
                "login": self.connection_parameters.login,
                "passcode": self.connection_parameters.unescaped_passcode,
            },
        )
        await self.connection.write_frame(ConnectFrame(headers=headers))
        collected: list[MessageFrame | ReceiptFrame | ErrorFrame | HeartbeatFrame] = []

        async def connected() -> ConnectedFrame:
            async for frame in self.connection.read_frames():
                if isinstance(frame, ConnectedFrame):
                    return frame
                collected.append(frame)
            msg = "connection ended during handshake"
            raise ConnectionError(msg)

        try:
            frame = await asyncio.wait_for(connected(), timeout=self.connection_confirmation_timeout)
        except TimeoutError:
            return ConnectionConfirmationTimeout(timeout=self.connection_confirmation_timeout, frames=collected)
        if frame.headers["version"] != self.protocol_version:
            return UnsupportedProtocolVersion(
                given_version=frame.headers["version"], supported_version=self.protocol_version
            )
        heartbeat = Heartbeat.from_header(frame.headers["heart-beat"])
        self.set_heartbeat_interval(heartbeat)
        return EstablishedConnectionResult(server_heartbeat=heartbeat)

    async def exit(self) -> None:
        for subscription in self.active_subscriptions.get_all():
            await subscription.unsubscribe()
        receipt_id = _make_receipt_id()
        await self.connection.write_frame(DisconnectFrame(headers={"receipt": receipt_id}))

        async def received() -> None:
            async for frame in self.connection.read_frames():
                if isinstance(frame, ReceiptFrame) and frame.headers["receipt-id"] == receipt_id:
                    return

        with suppress(TimeoutError):
            await asyncio.wait_for(received(), timeout=self.disconnect_confirmation_timeout)


def _make_receipt_id() -> str:
    return str(uuid4())


class ConnectionLifespanFactory(Protocol):
    def __call__(
        self,
        *,
        connection: AbstractConnection,
        connection_parameters: ConnectionParameters,
        set_heartbeat_interval: Callable[[Heartbeat], Any],
    ) -> AbstractConnectionLifespan: ...
