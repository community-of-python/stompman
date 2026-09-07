"""The original mutable Client API, adapted to the independent session core."""

from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from types import TracebackType
from typing import Any, ClassVar, Self

from stompman._compat import LegacyOptions
from stompman.connection_lifespan import ConnectionLifespan
from stompman.connection_manager import ConnectionManager
from stompman.core import Runtime
from stompman.errors import SubscriptionError
from stompman.frames import AckMode, MessageFrame, SendFrame
from stompman.subscription import AckableMessageFrame, ActiveSubscriptions, AutoAckSubscription, ManualAckSubscription
from stompman.transaction import Transaction


@dataclass(kw_only=True, slots=True)
class Client(LegacyOptions):
    """Compatibility facade. Runtime owns protocol, recovery, and delivery state."""

    PROTOCOL_VERSION: ClassVar = "1.2"
    _runtime: Runtime | None = field(default=None, init=False, repr=False, compare=False)
    _connection_manager: ConnectionManager = field(init=False)
    _active_subscriptions: ActiveSubscriptions = field(default_factory=ActiveSubscriptions, init=False)
    _active_transactions: set[Transaction] = field(default_factory=set, init=False)

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
            ssl=self.ssl,
        )
        self._connection_manager.bind(lambda: self.core)

    @property
    def core(self) -> Runtime:
        if self._runtime is None:
            self._runtime = self.to_runtime(wrap_transport=self._connection_manager.observe_transport)
        return self._runtime

    async def __aenter__(self) -> Self:
        await self._connection_manager.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            if exc_type is None:
                await self._active_subscriptions.wait_until_empty()
        except BaseException as error:
            await self._connection_manager.__aexit__(type(error), error, error.__traceback__)
            raise
        else:
            await self._connection_manager.__aexit__(exc_type, exc_value, traceback)
        finally:
            for subscription in self._active_subscriptions.get_all():
                self._active_subscriptions.delete_by_id(subscription.id)
            self._active_transactions.clear()

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
    ) -> AutoAckSubscription:
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
    ) -> ManualAckSubscription:
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
        return self._runtime is not None and self._runtime.is_alive()
