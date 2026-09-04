from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field, fields, replace
from types import TracebackType
from typing import Any, ClassVar, Self, overload

from stompman.core import Delivery, Runtime, RuntimeConfig
from stompman.frames import AckMode, MessageFrame, ReceiptFrame
from stompman.subscription import AckableMessageFrame, AutoAckSubscription, ManualAckSubscription, _make_subscription_id
from stompman.transaction import Transaction, _make_transaction_id


@dataclass(kw_only=True, slots=True)
class Client(RuntimeConfig):
    """Compatibility facade. Runtime owns all protocol and connection state."""

    PROTOCOL_VERSION: ClassVar = "1.2"
    _runtime: Runtime = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._runtime = Runtime(self.to_config())

    def to_config(self) -> RuntimeConfig:
        """Copy configuration without sharing lifecycle state with another adapter."""
        values = {item.name: getattr(self, item.name) for item in fields(RuntimeConfig)}
        values["servers"] = [replace(server, connect_headers=server.connect_headers.copy()) for server in self.servers]
        return RuntimeConfig(**values)

    @property
    def core(self) -> Runtime:
        return self._runtime

    async def __aenter__(self) -> Self:
        await self._runtime.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            if exc_type is None:
                await self._runtime.wait_until_unsubscribed()
        except BaseException as error:
            await self._runtime.close(type(error), error, error.__traceback__)
            raise
        else:
            await self._runtime.close(exc_type, exc_value, traceback)

    @overload
    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        receipt_timeout: None = None,
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
        receipt_timeout: float,
    ) -> ReceiptFrame: ...

    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        receipt_timeout: float | None = None,
    ) -> ReceiptFrame | None:
        return await self._runtime.send(
            body,
            destination,
            content_type=content_type,
            add_content_length=add_content_length,
            headers=headers,
            receipt_timeout=receipt_timeout,
        )

    def begin(self, *, receipt_timeout: float | None = None) -> Transaction:
        return Transaction(self._runtime.begin(receipt_timeout=receipt_timeout, transaction_id=_make_transaction_id()))

    async def subscribe(
        self,
        destination: str,
        handler: Callable[[MessageFrame], Awaitable[Any]],
        *,
        ack: AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        on_suppressed_exception: Callable[[Exception, MessageFrame], Any],
        suppressed_exception_classes: tuple[type[Exception], ...] = (Exception,),
    ) -> AutoAckSubscription:
        async def consume(delivery: Delivery) -> None:
            frame = MessageFrame(headers=delivery.headers, body=delivery.body)
            try:
                await handler(frame)
            except suppressed_exception_classes as error:
                if ack != "auto":
                    await delivery.nack()
                on_suppressed_exception(error, frame)
            else:
                if ack != "auto":
                    await delivery.ack()

        subscription = await self._runtime.subscribe(
            destination, consume, ack=ack, headers=headers, subscription_id=_make_subscription_id()
        )
        return AutoAckSubscription(subscription, handler, on_suppressed_exception, suppressed_exception_classes)

    async def subscribe_with_manual_ack(
        self,
        destination: str,
        handler: Callable[[AckableMessageFrame], Coroutine[Any, Any, Any]],
        *,
        ack: AckMode = "client-individual",
        headers: dict[str, str] | None = None,
    ) -> ManualAckSubscription:
        async def consume(delivery: Delivery) -> None:
            await handler(AckableMessageFrame.from_delivery(delivery))

        subscription = await self._runtime.subscribe(
            destination, consume, ack=ack, headers=headers, subscription_id=_make_subscription_id()
        )
        return ManualAckSubscription(subscription, handler)

    def is_alive(self) -> bool:
        return self._runtime.is_alive()
