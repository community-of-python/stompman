"""Native facade: compose owners and enforce the running/draining lifecycle."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self, overload
from uuid import uuid4

from ._tasks import await_cleanup
from .config import DEFAULT_CONFIRMATION, Confirmation, Confirmed, Heartbeat, RuntimeConfig, Server, Unconfirmed
from .connector import Connector
from .delivery import Deliveries, Delivery
from .errors import SubscriptionError
from .frames import AckMode, AnyClientFrame, AnyServerFrame, ErrorFrame, MessageFrame, ReceiptFrame, SendFrame
from .recovery import Connected, ConnectionSupervisor, Failed, Restoring
from .session import Session
from .subscriptions import Subscription, Subscriptions, SubscriptionSpec, log_subscription_error
from .transaction import Transaction
from .transport import TransportFactory, connect_tcp


def log_error_frame(frame: ErrorFrame) -> None:
    logging.getLogger("stompman").error("received error frame: %s", frame)


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: Literal["closed", "connecting", "connected", "recovering", "failed"]
    generation: int
    heartbeat: Heartbeat
    pending_messages: int
    pending_bytes: int
    running_handlers: int
    failure: Exception | None
    subscription_ids: tuple[str, ...]
    pending_receipts: int
    writing: bool


@dataclass(frozen=True, slots=True)
class Stopped:
    generation: int


@dataclass(frozen=True, slots=True)
class Running:
    connections: ConnectionSupervisor
    subscriptions: Subscriptions
    deliveries: Deliveries
    group: asyncio.TaskGroup
    supervisor: asyncio.Task[None]
    dispatcher: asyncio.Task[None]


@dataclass(frozen=True, slots=True)
class Draining:
    running: Running


class Runtime:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        transport_factory: TransportFactory = connect_tcp,
        on_error_frame: Callable[[ErrorFrame], Any] = log_error_frame,
        server_source: Callable[[], tuple[Server, ...]] | None = None,
    ) -> None:
        self._config = config
        self._connector = Connector(
            (lambda: config.servers) if server_source is None else server_source, config.connection, transport_factory
        )
        self._on_error = on_error_frame
        self._state: Stopped | Running | Draining = Stopped(0)
        self._lifecycle = asyncio.Lock()

    @property
    def config(self) -> RuntimeConfig:
        return self._config

    @property
    def status(self) -> RuntimeStatus:
        state = self._state
        if isinstance(state, Stopped):
            return RuntimeStatus("closed", state.generation, Heartbeat(0, 0), 0, 0, 0, None, (), 0, writing=False)
        running = state.running if isinstance(state, Draining) else state
        connections, deliveries = running.connections, running.deliveries
        connection = connections.state
        phase: Literal["connected", "recovering", "failed"] = "recovering"
        if isinstance(connection, Failed):
            phase = "failed"
        elif isinstance(connection, Connected) and not connection.session.ended.done():
            phase = "connected"
        return RuntimeStatus(
            phase,
            connections.generation,
            connections.heartbeat,
            deliveries.pending_messages,
            deliveries.pending_bytes,
            deliveries.running_handlers,
            connection.error if isinstance(connection, Failed) else None,
            running.subscriptions.ids,
            connection.session.receipts.pending_count if isinstance(connection, (Restoring, Connected)) else 0,
            connection.session.writing if isinstance(connection, (Restoring, Connected)) else False,
        )

    def _running(self) -> Running:
        if isinstance(self._state, Running):
            return self._state
        if isinstance(self._state, Draining) and self._state.running.deliveries.is_handler():
            return self._state.running
        msg = "runtime is not running"
        raise RuntimeError(msg)

    def is_alive(self) -> bool:
        return isinstance(self._state, Running) and self._state.connections.is_alive()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.close(exc_type, exc_value, traceback)

    async def start(self) -> None:
        async with self._lifecycle:
            if not isinstance(self._state, Stopped):
                return
            group = asyncio.TaskGroup()
            deliveries = Deliveries(self.config.delivery, group)

            def receive(frame: AnyServerFrame, session: Session) -> None:
                if isinstance(frame, ErrorFrame):
                    self._on_error(frame)
                elif isinstance(frame, MessageFrame):
                    subscriptions.receive(frame, session)

            connections = ConnectionSupervisor(self.config, self._connector, receive, self._state.generation)
            subscriptions = Subscriptions(connections, deliveries)
            try:
                await connections.start()
            except BaseException:
                await await_cleanup(asyncio.create_task(connections.close(graceful=False)))
                raise
            await group.__aenter__()
            self._state = Running(
                connections,
                subscriptions,
                deliveries,
                group,
                group.create_task(connections.supervise(), name="stomp-recovery"),
                group.create_task(deliveries.dispatch(), name="stomp-delivery"),
            )

    async def close(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
        *,
        cancel_handlers: bool = False,
    ) -> None:
        del exc_type, traceback
        async with self._lifecycle:
            if isinstance(self._state, Stopped):
                return
            running = self._state.running if isinstance(self._state, Draining) else self._state
            self._state = Draining(running)
            running.dispatcher.cancel()
            running.subscriptions.pause()
            failed = isinstance(running.connections.state, Failed)
            try:
                await running.deliveries.drain(cancel=cancel_handlers or exc_value is not None or failed)
                await running.subscriptions.close()
            finally:
                running.supervisor.cancel()
                try:
                    await running.group.__aexit__(None, None, None)
                finally:
                    try:
                        await await_cleanup(
                            asyncio.create_task(running.connections.close(graceful=exc_value is None and not failed))
                        )
                    finally:
                        self._state = Stopped(running.connections.generation)

    async def wait_until_unsubscribed(self) -> None:
        if not isinstance(self._state, Stopped):
            await self._running().subscriptions.wait_empty()

    async def reconnect(self) -> None:
        await self._running().connections.reconnect()

    async def ensure_connected(self) -> None:
        """Wait for a ready generation, preserving an already healthy session."""
        await self._running().connections.start()

    async def write_frame(
        self, frame: AnyClientFrame, confirmation: Confirmation = DEFAULT_CONFIRMATION
    ) -> ReceiptFrame | None:
        return await self._running().connections.write(frame, confirmation)

    async def submit_if_connected(self, frame: AnyClientFrame, confirmation: Confirmation) -> bool:
        command = await self._running().connections.submit_current(frame, confirmation)
        if command is None:
            return False
        await command.complete()
        return True

    async def subscribe_from(self, source: Callable[[], SubscriptionSpec]) -> Subscription:
        """Install immutable snapshots supplied at creation and each restoration."""
        return await self._running().subscriptions.follow(source)

    @overload
    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        confirmation: Confirmed = DEFAULT_CONFIRMATION,
    ) -> ReceiptFrame: ...

    @overload
    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        confirmation: Unconfirmed,
    ) -> None: ...

    @overload
    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        confirmation: Confirmation,
    ) -> ReceiptFrame | None: ...

    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        confirmation: Confirmation = DEFAULT_CONFIRMATION,
    ) -> ReceiptFrame | None:
        frame = SendFrame.build(
            body=body,
            destination=destination,
            transaction=None,
            content_type=content_type,
            add_content_length=add_content_length,
            headers=headers,
        )
        return await self._running().connections.write(frame, confirmation)

    def begin(
        self,
        *,
        confirmation: Confirmation = DEFAULT_CONFIRMATION,
        transaction_id: str = "",
        commit_confirmation: Confirmation = DEFAULT_CONFIRMATION,
        replay_source: Callable[[], tuple[SendFrame, ...]] | None = None,
    ) -> Transaction:
        return Transaction(
            self._running().connections,
            transaction_id or str(uuid4()),
            confirmation,
            commit_confirmation,
            replay_source=replay_source,
        )

    async def subscribe(
        self,
        destination: str,
        handler: Callable[[Delivery], Awaitable[Any]],
        *,
        ack: AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        subscription_id: str = "",
        confirmation: Confirmation = DEFAULT_CONFIRMATION,
        operation_confirmation: Confirmation = DEFAULT_CONFIRMATION,
        on_subscription_error: Callable[[SubscriptionError], Any] = log_subscription_error,
    ) -> Subscription:
        spec = SubscriptionSpec(
            subscription_id or str(uuid4()),
            destination,
            ack,
            headers or {},
            handler,
            confirmation,
            operation_confirmation,
            on_subscription_error,
        )
        return await self._running().subscriptions.subscribe(spec)
