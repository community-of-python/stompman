"""Mutable subscription contracts backed by immutable core specifications."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Self, cast
from uuid import uuid4

from stompman._legacy_errors import translate_connection_errors
from stompman.connection import AbstractConnection
from stompman.connection_manager import ConnectionManager
from stompman.core.config import Confirmed, Unconfirmed
from stompman.core.delivery import Delivery
from stompman.core.subscriptions import Subscription, SubscriptionSpec, log_subscription_error
from stompman.errors import ConnectionLostError, FailedAllWriteAttemptsError, SubscriptionError
from stompman.frames import AckFrame, AckMode, MessageFrame, NackFrame, SubscribeFrame, UnsubscribeFrame
from stompman.logger import LOGGER

_current_delivery: ContextVar[Delivery | None] = ContextVar("stompman_legacy_delivery", default=None)


@dataclass(kw_only=True, slots=True, frozen=True)
class ActiveSubscriptions:
    subscriptions: dict[str, "AutoAckSubscription | ManualAckSubscription"] = field(default_factory=dict, init=False)
    event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        self.event.set()

    def get_by_id(self, subscription_id: str) -> "AutoAckSubscription | ManualAckSubscription | None":
        return self.subscriptions.get(subscription_id)

    def get_all(self) -> list["AutoAckSubscription | ManualAckSubscription"]:
        return list(self.subscriptions.values())

    def get_ids(self) -> list[str]:
        return list(self.subscriptions)

    def delete_by_id(self, subscription_id: str) -> None:
        self.subscriptions.pop(subscription_id, None)
        if not self.subscriptions:
            self.event.set()

    def add(self, subscription: "AutoAckSubscription | ManualAckSubscription") -> None:
        self.subscriptions[subscription.id] = subscription
        self.event.clear()

    def contains_by_id(self, subscription_id: str) -> bool:
        return subscription_id in self.subscriptions

    async def wait_until_empty(self) -> bool:
        return await self.event.wait()


@dataclass(kw_only=True, slots=True)
class BaseSubscription:
    id: str = field(default_factory=lambda: _make_subscription_id(), init=False)  # ruff: ignore[unnecessary-lambda]
    destination: str
    headers: dict[str, str] | None
    ack: AckMode
    _connection_manager: ConnectionManager
    _active_subscriptions: ActiveSubscriptions
    receipt_timeout: float | None = None
    on_subscription_error: Callable[[SubscriptionError], Any] | None = None
    _subscription: Subscription | None = field(default=None, init=False, repr=False, compare=False)

    def _spec(self) -> SubscriptionSpec:
        for key, subscription in tuple(self._active_subscriptions.subscriptions.items()):
            if subscription is self and key != self.id:
                self._active_subscriptions.delete_by_id(key)
        self._active_subscriptions.add(cast("AutoAckSubscription | ManualAckSubscription", self))
        operations = Unconfirmed(self._connection_manager.write_retry_attempts)
        return SubscriptionSpec(
            self.id,
            self.destination,
            self.ack,
            self.headers or {},
            self._consume,
            operations if self.receipt_timeout is None else Confirmed(self.receipt_timeout),
            operations,
            self._on_error,
        )

    async def _consume(self, delivery: Delivery) -> None:
        raise NotImplementedError

    def _on_error(self, error: SubscriptionError) -> None:
        self._active_subscriptions.delete_by_id(self.id)
        (self.on_subscription_error or log_subscription_error)(error)

    async def _subscribe(self) -> None:
        attempts = self._connection_manager.write_retry_attempts
        if attempts <= 0:
            raise FailedAllWriteAttemptsError(retry_attempts=attempts)
        with translate_connection_errors(
            self._connection_manager.servers, timeout=self._connection_manager.connect_timeout
        ):
            try:
                self._subscription = await self._connection_manager.runtime.subscribe_from(self._spec)
            except BaseException as error:
                self._active_subscriptions.delete_by_id(self.id)
                if isinstance(error, ConnectionLostError) and self.receipt_timeout is None:
                    raise FailedAllWriteAttemptsError(retry_attempts=attempts) from error
                raise

    async def unsubscribe(self) -> None:
        self._active_subscriptions.delete_by_id(self.id)
        if self._subscription is None:
            await self._connection_manager.maybe_write_frame(UnsubscribeFrame(headers={"id": self.id}))
        else:
            await self._subscription.unsubscribe()

    async def _resubscribe(self, connection: AbstractConnection) -> None:
        if self._active_subscriptions.contains_by_id(self.id):
            await connection.write_frame(
                SubscribeFrame.build(
                    subscription_id=self.id, destination=self.destination, ack=self.ack, headers=self.headers
                )
            )

    async def _settle(self, frame: MessageFrame, epoch: int, *, accepted: bool) -> None:
        delivery = frame._delivery if isinstance(frame, AckableMessageFrame) else _current_delivery.get()
        if delivery is not None:
            await (delivery.ack() if accepted else delivery.nack())
            return
        if not self._active_subscriptions.contains_by_id(self.id):
            LOGGER.warning("failed to settle message frame: subscription is not active")
            return
        if epoch != self._connection_manager._reconnection_count:
            LOGGER.warning("skipping acknowledgement: connection changed since message was received")
            return
        ack_id = frame.headers.get("ack")
        if not ack_id:
            LOGGER.warning("failed to settle message frame: it has no ack header")
            return
        frame_type = AckFrame if accepted else NackFrame
        await self._connection_manager.maybe_write_frame(frame_type(headers={"id": ack_id, "subscription": self.id}))

    async def _ack(self, frame: MessageFrame, *, received_at_reconnection_count: int) -> None:
        await self._settle(frame, received_at_reconnection_count, accepted=True)

    async def _nack(self, frame: MessageFrame, *, received_at_reconnection_count: int) -> None:
        await self._settle(frame, received_at_reconnection_count, accepted=False)


@dataclass(kw_only=True, slots=True)
class AutoAckSubscription(BaseSubscription):
    handler: Callable[[MessageFrame], Awaitable[Any]]
    on_suppressed_exception: Callable[[Exception, MessageFrame], Any]
    suppressed_exception_classes: tuple[type[Exception], ...]
    _should_handle_ack_nack: bool = field(init=False)

    def __post_init__(self) -> None:
        self._should_handle_ack_nack = self.ack in {"client", "client-individual"}

    async def _consume(self, delivery: Delivery) -> None:
        token = _current_delivery.set(delivery)
        try:
            await self._run_handler(
                frame=MessageFrame(headers=delivery.headers, body=delivery.body),
                received_at_reconnection_count=self._connection_manager._reconnection_count,
            )
        finally:
            _current_delivery.reset(token)

    async def _run_handler(self, *, frame: MessageFrame, received_at_reconnection_count: int) -> None:
        try:
            await self.handler(frame)
        except self.suppressed_exception_classes as exception:
            if self._should_handle_ack_nack:
                await self._nack(frame, received_at_reconnection_count=received_at_reconnection_count)
            self.on_suppressed_exception(exception, frame)
        else:
            if self._should_handle_ack_nack:
                await self._ack(frame, received_at_reconnection_count=received_at_reconnection_count)


@dataclass(kw_only=True, slots=True)
class ManualAckSubscription(BaseSubscription):
    handler: Callable[["AckableMessageFrame"], Coroutine[Any, Any, Any]]

    async def _consume(self, delivery: Delivery) -> None:
        await self.handler(AckableMessageFrame.from_delivery(delivery, subscription=self))


@dataclass(frozen=True, kw_only=True, slots=True)
class AckableMessageFrame(MessageFrame):
    _subscription: ManualAckSubscription
    _received_at_reconnection_count: int
    _delivery: Delivery | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_delivery(cls, delivery: Delivery, *, subscription: ManualAckSubscription | None = None) -> Self:
        # Native facade callers have a settlement capability but no legacy subscription object.
        owner = (
            subscription
            if subscription is not None
            else cast("ManualAckSubscription", _NativeAcknowledgement(delivery))
        )
        return cls(
            headers=delivery.headers,
            body=delivery.body,
            _subscription=owner,
            _received_at_reconnection_count=0
            if subscription is None
            else subscription._connection_manager._reconnection_count,
            _delivery=delivery,
        )

    async def ack(self) -> None:
        await self._subscription._ack(self, received_at_reconnection_count=self._received_at_reconnection_count)

    async def nack(self) -> None:
        await self._subscription._nack(self, received_at_reconnection_count=self._received_at_reconnection_count)


@dataclass(frozen=True, slots=True)
class _NativeAcknowledgement:
    delivery: Delivery

    async def _ack(self, frame: MessageFrame, *, received_at_reconnection_count: int) -> None:
        del frame, received_at_reconnection_count
        await self.delivery.ack()

    async def _nack(self, frame: MessageFrame, *, received_at_reconnection_count: int) -> None:
        del frame, received_at_reconnection_count
        await self.delivery.nack()


def _make_subscription_id() -> str:
    return str(uuid4())


async def resubscribe_to_active_subscriptions(
    *, connection: AbstractConnection, active_subscriptions: ActiveSubscriptions
) -> None:
    for subscription in active_subscriptions.get_all():
        await subscription._resubscribe(connection)


async def unsubscribe_from_all_active_subscriptions(*, active_subscriptions: ActiveSubscriptions) -> None:
    for subscription in active_subscriptions.get_all():
        await subscription.unsubscribe()
