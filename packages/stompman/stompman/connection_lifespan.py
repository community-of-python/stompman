import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import uuid4

from stompman.config import ConnectionParameters, Heartbeat
from stompman.connection import AbstractConnection
from stompman.errors import ConnectionConfirmationTimeout, StompProtocolConnectionIssue, UnsupportedProtocolVersion
from stompman.frames import (
    ConnectedFrame,
    ConnectFrame,
    ConnectHeaders,
    DisconnectFrame,
    ReceiptFrame,
)
from stompman.subscription import ActiveSubscriptions, unsubscribe_from_all_active_subscriptions


@dataclass(frozen=True, kw_only=True, slots=True)
class EstablishedConnectionResult:
    server_heartbeat: Heartbeat


class AbstractConnectionLifespan(Protocol):
    connection_parameters: ConnectionParameters

    async def enter(self) -> EstablishedConnectionResult | StompProtocolConnectionIssue: ...
    async def exit(self) -> None: ...


@dataclass(kw_only=True, slots=True)
class ConnectionLifespan(AbstractConnectionLifespan):
    connection: AbstractConnection
    connection_parameters: ConnectionParameters
    protocol_version: str
    client_heartbeat: Heartbeat
    connection_confirmation_timeout: int
    disconnect_confirmation_timeout: int
    active_subscriptions: ActiveSubscriptions
    set_heartbeat_interval: Callable[[Heartbeat], Any]

    async def enter(self) -> EstablishedConnectionResult | StompProtocolConnectionIssue:
        connect_headers = cast(
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
        await self.connection.write_frame(ConnectFrame(headers=connect_headers))
        collected_frames = []

        async def take_connected_frame_and_collect_other_frames() -> ConnectedFrame:
            async for frame in self.connection.read_frames():
                if isinstance(frame, ConnectedFrame):
                    return frame
                collected_frames.append(frame)
            msg = "unreachable"  # pragma: no cover
            raise AssertionError(msg)  # pragma: no cover

        try:
            connected_frame = await asyncio.wait_for(
                take_connected_frame_and_collect_other_frames(), timeout=self.connection_confirmation_timeout
            )
        except TimeoutError:
            return ConnectionConfirmationTimeout(timeout=self.connection_confirmation_timeout, frames=collected_frames)

        if connected_frame.headers["version"] != self.protocol_version:
            return UnsupportedProtocolVersion(
                given_version=connected_frame.headers["version"], supported_version=self.protocol_version
            )

        server_heartbeat = Heartbeat.from_header(connected_frame.headers["heart-beat"])
        self.set_heartbeat_interval(server_heartbeat)
        return EstablishedConnectionResult(server_heartbeat=server_heartbeat)

    async def _take_receipt_frame(self) -> None:
        async for frame in self.connection.read_frames():
            if isinstance(frame, ReceiptFrame):
                break

    async def exit(self) -> None:
        await unsubscribe_from_all_active_subscriptions(active_subscriptions=self.active_subscriptions)
        await self.connection.write_frame(DisconnectFrame(headers={"receipt": _make_receipt_id()}))

        with suppress(TimeoutError):
            await asyncio.wait_for(self._take_receipt_frame(), timeout=self.disconnect_confirmation_timeout)


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
