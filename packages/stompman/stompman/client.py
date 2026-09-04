import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from ssl import SSLContext
from types import TracebackType
from typing import Any, ClassVar, Literal, Self

from stompman.config import ConnectionParameters, Heartbeat
from stompman.connection import AbstractConnection, Connection
from stompman.connection_lifespan import ConnectionLifespan
from stompman.connection_manager import ConnectionManager
from stompman.errors import SubscriptionError
from stompman.frames import (
    AckMode,
    ConnectedFrame,
    ErrorFrame,
    HeartbeatFrame,
    MessageFrame,
    ReceiptFrame,
    SendFrame,
)
from stompman.logger import LOGGER
from stompman.subscription import (
    AckableMessageFrame,
    ActiveSubscriptions,
    AutoAckSubscription,
    ManualAckSubscription,
    resubscribe_to_active_subscriptions,
)
from stompman.transaction import Transaction, commit_pending_transactions


async def _run_handler_with_safety_net(coro: Coroutine[Any, Any, Any]) -> None:
    try:
        await coro
    except Exception:  # ruff: ignore[blind-except]
        LOGGER.exception("unhandled exception in message handler")


@dataclass(kw_only=True, slots=True)
class Client:
    PROTOCOL_VERSION: ClassVar = "1.2"  # https://stomp.github.io/stomp-specification-1.2.html

    servers: list[ConnectionParameters] = field(kw_only=False)
    on_error_frame: Callable[[ErrorFrame], Any] | None = lambda error_frame: LOGGER.error(
        "received error frame: %s", error_frame
    )

    heartbeat: Heartbeat = field(default=Heartbeat(1000, 1000))
    ssl: Literal[True] | SSLContext | None = None
    connect_retry_attempts: int = 3
    connect_retry_interval: int = 1
    connect_timeout: int = 2
    read_max_chunk_size: int = 1024 * 1024
    write_retry_attempts: int = 3
    connection_confirmation_timeout: int = 2
    disconnect_confirmation_timeout: int = 2
    check_server_alive_interval_factor: int = 3
    """Client will check if server alive `server heartbeat interval` times `interval factor`"""
    no_message_restart_interval: timedelta | None = timedelta(hours=1)
    """Force reconnect if no messages received within this interval. None to disable."""
    keep_alive_on_connection_failure: bool = False
    """Keep background connection recovery alive after a retry cycle is exhausted."""
    max_concurrent_handlers: int | None = 100
    """Cap on concurrently-running message handlers. Set to None to disable the cap."""

    connection_class: type[AbstractConnection] = Connection

    _connection_manager: ConnectionManager = field(init=False)
    _active_subscriptions: ActiveSubscriptions = field(default_factory=ActiveSubscriptions, init=False)
    _active_transactions: set[Transaction] = field(default_factory=set, init=False)
    _exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)
    _listen_task: asyncio.Task[None] = field(init=False, repr=False)
    _task_group: asyncio.TaskGroup = field(init=False, repr=False)
    _handler_semaphore: asyncio.Semaphore | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._connection_manager = ConnectionManager(
            servers=self.servers,
            lifespan_factory=partial(
                ConnectionLifespan,
                protocol_version=self.PROTOCOL_VERSION,
                client_heartbeat=self.heartbeat,
                connection_confirmation_timeout=self.connection_confirmation_timeout,
                disconnect_confirmation_timeout=self.disconnect_confirmation_timeout,
                active_subscriptions=self._active_subscriptions,
            ),
            connection_class=self.connection_class,
            connect_retry_attempts=self.connect_retry_attempts,
            connect_retry_interval=self.connect_retry_interval,
            connect_timeout=self.connect_timeout,
            read_max_chunk_size=self.read_max_chunk_size,
            write_retry_attempts=self.write_retry_attempts,
            check_server_alive_interval_factor=self.check_server_alive_interval_factor,
            no_message_restart_interval=self.no_message_restart_interval,
            keep_alive_on_connection_failure=self.keep_alive_on_connection_failure,
            on_connection_lost=self._active_subscriptions.connection_lost,
            restore_connection=self._restore_connection,
            ssl=self.ssl,
        )
        if self.max_concurrent_handlers is not None:
            self._handler_semaphore = asyncio.Semaphore(self.max_concurrent_handlers)

    async def _restore_connection(self, connection: AbstractConnection) -> None:
        await resubscribe_to_active_subscriptions(
            connection=connection, active_subscriptions=self._active_subscriptions
        )
        await commit_pending_transactions(connection=connection, active_transactions=self._active_transactions)

    async def __aenter__(self) -> Self:
        self._task_group = await self._exit_stack.enter_async_context(asyncio.TaskGroup())
        await self._exit_stack.enter_async_context(self._connection_manager)
        self._listen_task = self._task_group.create_task(self._listen_to_frames())
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            if not exc_value:
                await self._active_subscriptions.wait_until_empty()
        finally:
            self._listen_task.cancel()
            await asyncio.wait([self._listen_task])
            await self._active_subscriptions.cancel_confirmation_tasks()
            await self._exit_stack.aclose()

    async def _listen_to_frames(self) -> None:
        async with asyncio.TaskGroup() as task_group:
            async for frame, epoch in self._connection_manager.read_frames_reconnecting():
                match frame:
                    case MessageFrame():
                        self._connection_manager._last_message_received_time = time.time()
                        if subscription := self._active_subscriptions.get_by_id(frame.headers["subscription"]):
                            reserved = await self._reserve_handler_slot()
                            task = task_group.create_task(
                                self._run_message_handler(subscription, frame, epoch=epoch, reserved=reserved)
                            )
                            if reserved and self._handler_semaphore is not None:
                                semaphore = self._handler_semaphore

                                def _release(_t: asyncio.Task[None], s: asyncio.Semaphore = semaphore) -> None:
                                    s.release()

                                task.add_done_callback(_release)
                    case ErrorFrame() | ReceiptFrame():
                        self._handle_subscription_frame(frame, epoch=epoch)
                    case HeartbeatFrame() | ConnectedFrame():
                        pass

    async def _reserve_handler_slot(self) -> bool:
        semaphore = self._handler_semaphore
        if semaphore is None:
            return False
        if not semaphore.locked():
            await semaphore.acquire()
            return True
        if self._active_subscriptions.pending_receipts:
            return False
        capacity = asyncio.create_task(semaphore.acquire())
        confirmation = asyncio.create_task(self._active_subscriptions.confirmation_started.wait())
        reserved = False
        try:
            await asyncio.wait((capacity, confirmation), return_when=asyncio.FIRST_COMPLETED)
            if capacity.done() and not capacity.cancelled():
                reserved = capacity.result()
            return reserved
        finally:
            confirmation.cancel()
            if not capacity.done():
                capacity.cancel()
            await asyncio.gather(capacity, confirmation, return_exceptions=True)
            if not reserved and not capacity.cancelled() and capacity.result():
                semaphore.release()

    async def _run_message_handler(
        self,
        subscription: AutoAckSubscription | ManualAckSubscription,
        frame: MessageFrame,
        *,
        epoch: int,
        reserved: bool,
    ) -> None:
        if self._handler_semaphore is not None and not reserved:
            async with self._handler_semaphore:
                await self._invoke_message_handler(subscription, frame, epoch=epoch)
        else:
            await self._invoke_message_handler(subscription, frame, epoch=epoch)

    @staticmethod
    async def _invoke_message_handler(
        subscription: AutoAckSubscription | ManualAckSubscription, frame: MessageFrame, *, epoch: int
    ) -> None:
        handler = (
            subscription._run_handler(frame=frame, received_at_reconnection_count=epoch)
            if isinstance(subscription, AutoAckSubscription)
            else subscription.handler(
                AckableMessageFrame(
                    headers=frame.headers,
                    body=frame.body,
                    _subscription=subscription,
                    _received_at_reconnection_count=epoch,
                )
            )
        )
        await _run_handler_with_safety_net(handler)

    def _handle_subscription_frame(self, frame: ErrorFrame | ReceiptFrame, *, epoch: int) -> None:
        if isinstance(frame, ReceiptFrame):
            self._active_subscriptions.handle_receipt(frame, epoch=epoch)
        else:
            self._active_subscriptions.handle_error(frame, epoch=epoch)
            if self.on_error_frame:
                self.on_error_frame(frame)

    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        await self._connection_manager.write_frame_reconnecting(
            SendFrame.build(
                body=body,
                destination=destination,
                transaction=None,
                content_type=content_type,
                add_content_length=add_content_length,
                headers=headers,
            )
        )

    @asynccontextmanager
    async def begin(self) -> AsyncGenerator[Transaction, None]:
        async with Transaction(
            _connection_manager=self._connection_manager, _active_transactions=self._active_transactions
        ) as transaction:
            yield transaction

    async def subscribe(
        self,
        destination: str,
        handler: Callable[[MessageFrame], Awaitable[Any]],
        *,
        ack: AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        on_suppressed_exception: Callable[[Exception, MessageFrame], Any],
        suppressed_exception_classes: tuple[type[Exception], ...] = (Exception,),
        receipt_timeout: float | None = None,
        on_subscription_error: Callable[[SubscriptionError], Any] | None = None,
    ) -> "AutoAckSubscription":
        subscription = AutoAckSubscription(
            destination=destination,
            handler=handler,
            headers=headers,
            ack=ack,
            on_suppressed_exception=on_suppressed_exception,
            suppressed_exception_classes=suppressed_exception_classes,
            receipt_timeout=receipt_timeout,
            on_subscription_error=on_subscription_error,
            _connection_manager=self._connection_manager,
            _active_subscriptions=self._active_subscriptions,
        )
        await subscription._subscribe()
        return subscription

    async def subscribe_with_manual_ack(
        self,
        destination: str,
        handler: Callable[[AckableMessageFrame], Coroutine[Any, Any, Any]],
        *,
        ack: AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        receipt_timeout: float | None = None,
        on_subscription_error: Callable[[SubscriptionError], Any] | None = None,
    ) -> "ManualAckSubscription":
        subscription = ManualAckSubscription(
            destination=destination,
            handler=handler,
            headers=headers,
            ack=ack,
            receipt_timeout=receipt_timeout,
            on_subscription_error=on_subscription_error,
            _connection_manager=self._connection_manager,
            _active_subscriptions=self._active_subscriptions,
        )
        await subscription._subscribe()
        return subscription

    def is_alive(self) -> bool:
        if self._listen_task.done():
            return False
        return (
            self._connection_manager._active_connection_state or False
        ) and self._connection_manager._active_connection_state.is_alive(self.check_server_alive_interval_factor)
