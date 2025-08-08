import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from stompman.connection import AbstractConnection
from stompman.connection_manager import ConnectionManager
from stompman.frames import (
    AckFrame,
    AckMode,
    MessageFrame,
    NackFrame,
    SubscribeFrame,
    UnsubscribeFrame,
)
from stompman.logger import LOGGER


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
        return list(self.subscriptions.keys())

    def delete_by_id(self, subscription_id: str) -> None:
        if subscription_id in self.subscriptions:
            del self.subscriptions[subscription_id]
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
    id: str = field(default_factory=lambda: _make_subscription_id(), init=False)  # noqa: PLW0108
    destination: str
    headers: dict[str, str] | None
    ack: AckMode
    _connection_manager: ConnectionManager
    _active_subscriptions: ActiveSubscriptions

    async def _subscribe(self) -> None:
        await self._connection_manager.write_frame_reconnecting(
            SubscribeFrame.build(
                subscription_id=self.id, destination=self.destination, ack=self.ack, headers=self.headers
            )
        )
        self._active_subscriptions.add(self)  # type: ignore[arg-type]

    async def unsubscribe(self) -> None:
        self._active_subscriptions.delete_by_id(self.id)
        await self._connection_manager.maybe_write_frame(UnsubscribeFrame(headers={"id": self.id}))

    async def _nack(self, frame: MessageFrame) -> None:
        if not self._active_subscriptions.contains_by_id(self.id):
            LOGGER.warning(
                "failed to nack message frame: subscription is not active. "
                "message_id: %s, subscription_id: %s, active_subscriptions: %s",
                frame.headers["message-id"],
                self.id,
                self._active_subscriptions.get_ids(),
            )
            return
        if not (ack_id := frame.headers.get("ack")):
            LOGGER.warning(
                'failed to nack message frame: it has no "ack" header. "'
                "message_id: %s, subscription_id: %s, frame_header_names: %s",
                frame.headers["message-id"],
                self.id,
                frame.headers.keys(),
            )
            return
        await self._connection_manager.maybe_write_frame(NackFrame(headers={"id": ack_id, "subscription": self.id}))

    async def _ack(self, frame: MessageFrame) -> None:
        if not self._active_subscriptions.contains_by_id(self.id):
            LOGGER.warning(
                "failed to ack message frame: subscription is not active. "
                "message_id: %s, subscription_id: %s, active_subscriptions: %s",
                frame.headers["message-id"],
                self.id,
                self._active_subscriptions.get_ids(),
            )
            return
        if not (ack_id := frame.headers.get("ack")):
            LOGGER.warning(
                'failed to ack message frame: it has no "ack" header. "'
                "message_id: %s, subscription_id: %s, frame_header_names: %s",
                frame.headers["message-id"],
                self.id,
                frame.headers.keys(),
            )
            return
        await self._connection_manager.maybe_write_frame(AckFrame(headers={"id": ack_id, "subscription": self.id}))


@dataclass(kw_only=True, slots=True)
class AutoAckSubscription(BaseSubscription):
    handler: Callable[[MessageFrame], Awaitable[Any]]
    on_suppressed_exception: Callable[[Exception, MessageFrame], Any]
    suppressed_exception_classes: tuple[type[Exception], ...]
    _should_handle_ack_nack: bool = field(init=False)

    def __post_init__(self) -> None:
        self._should_handle_ack_nack = self.ack in {"client", "client-individual"}

    async def _run_handler(self, *, frame: MessageFrame) -> None:
        try:
            await self.handler(frame)
        except self.suppressed_exception_classes as exception:
            if self._should_handle_ack_nack:
                await self._nack(frame)
            self.on_suppressed_exception(exception, frame)
        else:
            if self._should_handle_ack_nack:
                await self._ack(frame)


@dataclass(kw_only=True, slots=True)
class ManualAckSubscription(BaseSubscription):
    handler: Callable[["AckableMessageFrame"], Coroutine[Any, Any, Any]]


@dataclass(frozen=True, kw_only=True, slots=True)
class AckableMessageFrame(MessageFrame):
    _subscription: ManualAckSubscription

    async def ack(self) -> None:
        await self._subscription._ack(self)  # noqa: SLF001

    async def nack(self) -> None:
        await self._subscription._nack(self)  # noqa: SLF001


def _make_subscription_id() -> str:
    return str(uuid4())


async def resubscribe_to_active_subscriptions(
    *, connection: AbstractConnection, active_subscriptions: ActiveSubscriptions
) -> None:
    for subscription in active_subscriptions.get_all():
        await connection.write_frame(
            SubscribeFrame.build(
                subscription_id=subscription.id,
                destination=subscription.destination,
                ack=subscription.ack,
                headers=subscription.headers,
            )
        )


async def unsubscribe_from_all_active_subscriptions(*, active_subscriptions: ActiveSubscriptions) -> None:
    for subscription in active_subscriptions.get_all():
        await subscription.unsubscribe()
