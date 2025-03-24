import asyncio
import inspect
import time
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
    on_heartbeat: Callable[[], Any] | Callable[[], Awaitable[Any]] | None = None

    heartbeat: Heartbeat = field(default=Heartbeat(1000, 1000))
    ssl: Literal[True] | SSLContext | None = None
    connect_retry_attempts: int = 3
    connect_retry_interval: int = 1
    connect_timeout: int = 2
    read_timeout: int = 2
    read_max_chunk_size: int = 1024 * 1024
    write_retry_attempts: int = 3
    connection_confirmation_timeout: int = 2
    disconnect_confirmation_timeout: int = 2
    check_server_alive_interval_factor: int = 3
    """Client will check if server alive `server heartbeat interval` times `interval factor`"""

    connection_class: type[AbstractConnection] = Connection

    _connection_manager: ConnectionManager = field(init=False)
    _active_subscriptions: ActiveSubscriptions = field(default_factory=dict, init=False)
    _active_transactions: set[Transaction] = field(default_factory=set, init=False)
    _exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)
    _send_heartbeat_task: asyncio.Task[None] = field(init=False)
    _check_server_heartbeat_task: asyncio.Task[None] = field(init=False)
    _listen_task: asyncio.Task[None] = field(init=False)
    _task_group: asyncio.TaskGroup = field(init=False)
    _on_heartbeat_is_async: bool = field(init=False)

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
                set_heartbeat_interval=self._restart_heartbeat_tasks,
            ),
            connection_class=self.connection_class,
            connect_retry_attempts=self.connect_retry_attempts,
            connect_retry_interval=self.connect_retry_interval,
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            read_max_chunk_size=self.read_max_chunk_size,
            write_retry_attempts=self.write_retry_attempts,
            check_server_alive_interval_factor=self.check_server_alive_interval_factor,
            ssl=self.ssl,
        )
        self._on_heartbeat_is_async = inspect.iscoroutinefunction(self.on_heartbeat) if self.on_heartbeat else False

    async def __aenter__(self) -> Self:
        self._task_group = await self._exit_stack.enter_async_context(asyncio.TaskGroup())
        # TODO: move this tasks to lifespan
        self._send_heartbeat_task = self._task_group.create_task(asyncio.sleep(0))
        self._check_server_heartbeat_task = self._task_group.create_task(asyncio.sleep(0))
        await self._exit_stack.enter_async_context(self._connection_manager)
        self._listen_task = self._task_group.create_task(self._listen_to_frames())
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            if self._active_subscriptions and not exc_value:
                await asyncio.Future()
        finally:
            self._listen_task.cancel()
            self._send_heartbeat_task.cancel()
            self._check_server_heartbeat_task.cancel()
            await asyncio.wait([self._listen_task, self._send_heartbeat_task, self._check_server_heartbeat_task])
            await self._exit_stack.aclose()

    def _restart_heartbeat_tasks(self, server_heartbeat: Heartbeat) -> None:
        self._send_heartbeat_task.cancel()
        self._check_server_heartbeat_task.cancel()
        print(server_heartbeat)
        self._send_heartbeat_task = self._task_group.create_task(
            self._send_heartbeats_forever(server_heartbeat.want_to_receive_interval_ms)
        )
        self._check_server_heartbeat_task = self._task_group.create_task(
            self._monitor_server_aliveness(server_heartbeat.will_send_interval_ms)
        )

    async def _send_heartbeats_forever(self, client_heartbeat_interval_ms: int) -> None:
        while True:
            await self._connection_manager.write_heartbeat_reconnecting()
            await asyncio.sleep(client_heartbeat_interval_ms / 1000)

    async def _monitor_server_aliveness(self, server_heartbeat_interval_ms: int) -> None:
        server_heartbeat_interval_seconds = server_heartbeat_interval_ms / 1000
        while True:
            await asyncio.sleep(server_heartbeat_interval_seconds * self.check_server_alive_interval_factor)
            last_read_time_ms = (
                self._connection_manager._active_connection_state
                and self._connection_manager._active_connection_state.connection.last_read_time_ms
            )
            if not last_read_time_ms:
                print("No last_Read_time_ms")
                continue
            if (t := time.time() - last_read_time_ms) > server_heartbeat_interval_seconds:
                print("reset time", t)
                self._connection_manager._active_connection_state = None
            else:
                print("no reset")

    async def _listen_to_frames(self) -> None:
        async with asyncio.TaskGroup() as task_group:
            async for frame in self._connection_manager.read_frames_reconnecting():
                match frame:
                    case MessageFrame():
                        if subscription := self._active_subscriptions.get(frame.headers["subscription"]):
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
                    case HeartbeatFrame():
                        if self.on_heartbeat is None:
                            pass
                        elif self._on_heartbeat_is_async:
                            task_group.create_task(self.on_heartbeat())  # type: ignore[arg-type]
                        else:
                            self.on_heartbeat()
                    case ConnectedFrame() | ReceiptFrame():
                        pass

    async def send(
        self, body: bytes, destination: str, *, content_type: str | None = None, headers: dict[str, str] | None = None
    ) -> None:
        await self._connection_manager.write_frame_reconnecting(
            SendFrame.build(
                body=body, destination=destination, transaction=None, content_type=content_type, headers=headers
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
