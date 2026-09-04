from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Self
from uuid import uuid4

from stompman.core.delivery import Delivery, Subscription
from stompman.frames import AckMode, MessageFrame


@dataclass(frozen=True, kw_only=True, slots=True)
class AckableMessageFrame(Delivery):
    """Keep the legacy class identity while settlement executes in the core."""

    @classmethod
    def from_delivery(cls, delivery: Delivery) -> Self:
        return cls(
            headers=delivery.headers,
            body=delivery.body,
            _subscription=delivery._subscription,
            _generation=delivery._generation,
            _sequence=delivery._sequence,
        )


@dataclass(slots=True)
class BaseSubscription:
    _subscription: Subscription

    @property
    def id(self) -> str:
        return self._subscription.id

    @property
    def destination(self) -> str:
        return self._subscription.destination

    @property
    def headers(self) -> dict[str, str] | None:
        return self._subscription.headers

    @property
    def ack(self) -> AckMode:
        return self._subscription.ack

    async def unsubscribe(self) -> None:
        await self._subscription.unsubscribe()


@dataclass(slots=True)
class AutoAckSubscription(BaseSubscription):
    handler: Callable[[MessageFrame], Awaitable[Any]]
    on_suppressed_exception: Callable[[Exception, MessageFrame], Any]
    suppressed_exception_classes: tuple[type[Exception], ...]


@dataclass(slots=True)
class ManualAckSubscription(BaseSubscription):
    handler: Callable[[AckableMessageFrame], Coroutine[Any, Any, Any]]


def _make_subscription_id() -> str:
    return str(uuid4())
