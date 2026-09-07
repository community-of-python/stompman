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
from .protocol import STOMP_12, Stomp12
from .recovery import ConnectionSupervisor
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
    _group: asyncio.TaskGroup
    _workers: tuple[asyncio.Task[None], asyncio.Task[None]]

    @classmethod
    async def open(
        cls,
        config: RuntimeConfig,
        connector: Connector,
        generation: int,
        on_error: Callable[[ErrorFrame], Any],
    ) -> Self:
        group = asyncio.TaskGroup()
        deliveries = Deliveries(config.delivery, group)

        def receive(frame: AnyServerFrame, session: Session) -> None:
            if isinstance(frame, ErrorFrame):
                on_error(frame)
            elif isinstance(frame, MessageFrame):
                subscriptions.receive(frame, session)

        connections = ConnectionSupervisor(config, connector, receive, generation)
        subscriptions = Subscriptions(connections, deliveries)
        try:
            await connections.start()
            await group.__aenter__()
        except BaseException:
            await await_cleanup(connections.close(graceful=False))
            raise
        workers = (
            group.create_task(connections.supervise(), name="stomp-recovery"),
            group.create_task(deliveries.dispatch(), name="stomp-delivery"),
        )
        return cls(connections, subscriptions, deliveries, group, workers)

    async def close(self, *, graceful: bool, cancel_handlers: bool) -> None:
        failed = self.connections.snapshot().failure is not None
        # Stop admission and discard queued work. Running handlers may still
        # publish and settle before their subscriptions are removed.
        self.subscriptions.stop()
        try:
            await self.deliveries.drain(cancel=cancel_handlers or not graceful or failed)
            await self.subscriptions.close()
        finally:
            for worker in self._workers:
                worker.cancel()
            try:
                await self._group.__aexit__(None, None, None)
            finally:
                await await_cleanup(self.connections.close(graceful=graceful and not failed))


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
        protocol: Stomp12 = STOMP_12,
    ) -> None:
        protocol.validate_settings(config.connection)
        self._config = config
        self._connector = Connector(
            (lambda: config.servers) if server_source is None else server_source,
            config.connection,
            transport_factory,
            protocol,
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
            return RuntimeStatus(
                state="closed",
                generation=state.generation,
                heartbeat=Heartbeat(0, 0),
                pending_messages=0,
                pending_bytes=0,
                running_handlers=0,
                failure=None,
                subscription_ids=(),
                pending_receipts=0,
                writing=False,
            )
        running = state.running if isinstance(state, Draining) else state
        connection = running.connections.snapshot()
        return RuntimeStatus(
            state=connection.state,
            generation=connection.generation,
            heartbeat=connection.heartbeat,
            pending_messages=running.deliveries.pending_messages,
            pending_bytes=running.deliveries.pending_bytes,
            running_handlers=running.deliveries.running_handlers,
            failure=connection.failure,
            subscription_ids=running.subscriptions.ids,
            pending_receipts=connection.pending_receipts,
            writing=connection.writing,
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
            self._state = await Running.open(self.config, self._connector, self._state.generation, self._on_error)

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
            try:
                await running.close(graceful=exc_value is None, cancel_handlers=cancel_handlers)
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
        return await self._running().connections.write_current(frame, confirmation)

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
            id=subscription_id or str(uuid4()),
            destination=destination,
            ack=ack,
            headers=headers or {},
            handler=handler,
            confirmation=confirmation,
            operations=operation_confirmation,
            on_error=on_subscription_error,
        )
        return await self._running().subscriptions.subscribe(spec)
