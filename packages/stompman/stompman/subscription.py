from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Self
from uuid import uuid4

from stompman.core.config import Confirmed
from stompman.core.delivery import Delivery
from stompman.core.subscriptions import Subscription
from stompman.errors import SubscriptionError
from stompman.frames import AckMode, MessageFrame


@dataclass(frozen=True, kw_only=True, slots=True)
class AckableMessageFrame(Delivery):
    """Keep the legacy class identity while settlement executes in the core."""

    @classmethod
    def from_delivery(cls, delivery: Delivery) -> Self:
        return cls(
            headers=delivery.headers,
            body=delivery.body,
            _settlement=delivery._settlement,
        )


@dataclass(slots=True)
class BaseSubscription:
    _subscription: Subscription
    _legacy_headers: dict[str, str] | None
    _legacy_callback: Callable[[SubscriptionError], Any] | None

    @property
    def id(self) -> str:
        return self._subscription.id

    @property
    def destination(self) -> str:
        return self._subscription.destination

    @property
    def headers(self) -> dict[str, str] | None:
        return self._legacy_headers

    @property
    def ack(self) -> AckMode:
        return self._subscription.ack

    @property
    def receipt_timeout(self) -> float | None:
        confirmation = self._subscription.spec.confirmation
        return confirmation.timeout if isinstance(confirmation, Confirmed) else None

    @property
    def on_subscription_error(self) -> Callable[[SubscriptionError], Any] | None:
        return self._legacy_callback

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
