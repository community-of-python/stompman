import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from ssl import SSLContext
from types import TracebackType
from typing import Any, ClassVar, Literal, Self

from stompman.config import ConnectionParameters, Heartbeat
from stompman.connection import AbstractConnection, Connection
from stompman.connection_lifespan import ConnectionLifespan
from stompman.connection_manager import ConnectionManager
from stompman.frames import (
    AckMode,
    ConnectedFrame,
    ErrorFrame,
    HeartbeatFrame,
    MessageFrame,
    ReceiptFrame,
    SendFrame,
)
from stompman.subscription import AckableMessageFrame, ActiveSubscriptions, AutoAckSubscription, ManualAckSubscription
from stompman.transaction import Transaction


@dataclass(kw_only=True, slots=True)
class Client:
    PROTOCOL_VERSION: ClassVar = "1.2"  # https://stomp.github.io/stomp-specification-1.2.html

    servers: list[ConnectionParameters] = field(kw_only=False)
    on_error_frame: Callable[[ErrorFrame], Any] | None = None

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

    connection_class: type[AbstractConnection] = Connection

    _connection_manager: ConnectionManager = field(init=False)
    _active_subscriptions: ActiveSubscriptions = field(default_factory=ActiveSubscriptions, init=False)
    _active_transactions: set[Transaction] = field(default_factory=set, init=False)
    _exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)
    _listen_task: asyncio.Task[None] = field(init=False, repr=False)
    _task_group: asyncio.TaskGroup = field(init=False, repr=False)

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
                active_transactions=self._active_transactions,
            ),
            connection_class=self.connection_class,
            connect_retry_attempts=self.connect_retry_attempts,
            connect_retry_interval=self.connect_retry_interval,
            connect_timeout=self.connect_timeout,
            read_max_chunk_size=self.read_max_chunk_size,
            write_retry_attempts=self.write_retry_attempts,
            check_server_alive_interval_factor=self.check_server_alive_interval_factor,
            ssl=self.ssl,
        )

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
            await self._exit_stack.aclose()

    async def _listen_to_frames(self) -> None:
        async with asyncio.TaskGroup() as task_group:
            async for frame in self._connection_manager.read_frames_reconnecting():
                match frame:
                    case MessageFrame():
                        if subscription := self._active_subscriptions.get_by_id(frame.headers["subscription"]):
                            task_group.create_task(
                                subscription._run_handler(frame=frame)  # noqa: SLF001
                                if isinstance(subscription, AutoAckSubscription)
                                else subscription.handler(
                                    AckableMessageFrame(
                                        headers=frame.headers, body=frame.body, _subscription=subscription
                                    )
                                )
                            )
                    case ErrorFrame():
                        if self.on_error_frame:
                            self.on_error_frame(frame)
                    case HeartbeatFrame() | ConnectedFrame() | ReceiptFrame():
                        pass

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
    ) -> "AutoAckSubscription":
        subscription = AutoAckSubscription(
            destination=destination,
            handler=handler,
            headers=headers,
            ack=ack,
            on_suppressed_exception=on_suppressed_exception,
            suppressed_exception_classes=suppressed_exception_classes,
            _connection_manager=self._connection_manager,
            _active_subscriptions=self._active_subscriptions,
        )
        await subscription._subscribe()  # noqa: SLF001
        return subscription

    async def subscribe_with_manual_ack(
        self,
        destination: str,
        handler: Callable[[AckableMessageFrame], Coroutine[Any, Any, Any]],
        *,
        ack: AckMode = "client-individual",
        headers: dict[str, str] | None = None,
    ) -> "ManualAckSubscription":
        subscription = ManualAckSubscription(
            destination=destination,
            handler=handler,
            headers=headers,
            ack=ack,
            _connection_manager=self._connection_manager,
            _active_subscriptions=self._active_subscriptions,
        )
        await subscription._subscribe()  # noqa: SLF001
        return subscription

    def is_alive(self) -> bool:
        return (
            self._connection_manager._active_connection_state or False  # noqa: SLF001
        ) and self._connection_manager._active_connection_state.is_alive(self.check_server_alive_interval_factor)  # noqa: SLF001
