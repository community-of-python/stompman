import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stompman.errors import SubscriptionError
from stompman.frames import AckMode, MessageFrame, ReceiptFrame, SubscribeFrame

if TYPE_CHECKING:
    from stompman.core.runtime import Runtime


@dataclass(frozen=True, kw_only=True, slots=True)
class Delivery(MessageFrame):
    _subscription: "Subscription"
    _generation: int
    _sequence: int

    async def ack(self) -> None:
        await self._subscription._runtime.settle(self, accepted=True)

    async def nack(self) -> None:
        await self._subscription._runtime.settle(self, accepted=False)


@dataclass(eq=False, kw_only=True, slots=True)
class PendingDelivery:
    delivery: Delivery
    size: int
    outcome: bool | None = None
    handler_done: bool = False
    wire_done: bool = False


@dataclass(kw_only=True, slots=True)
class PendingConfirmation:
    receipt_id: str
    generation: int
    operation: asyncio.Task[ReceiptFrame | None]
    received: bool = False
    failure: SubscriptionError | None = None


@dataclass(kw_only=True, slots=True)
class Subscription:
    id: str
    destination: str
    ack: AckMode
    headers: dict[str, str] | None
    handler: Callable[[Delivery], Awaitable[Any]]
    _runtime: "Runtime"
    receipt_timeout: float | None = None
    on_subscription_error: Callable[[SubscriptionError], Any] | None = None
    _confirmation: PendingConfirmation | None = field(default=None, init=False, repr=False)
    _unsettled: deque[PendingDelivery] = field(default_factory=deque, init=False, repr=False)
    _deliveries: dict[int, PendingDelivery] = field(default_factory=dict, init=False, repr=False)
    _next_sequence: int = field(default=0, init=False, repr=False)
    _paused: bool = field(default=False, init=False, repr=False)

    def frame(self) -> SubscribeFrame:
        return SubscribeFrame.build(
            subscription_id=self.id, destination=self.destination, ack=self.ack, headers=self.headers
        )

    def pause(self) -> None:
        """Stop admitting new handlers while allowing in-flight settlement."""
        self._runtime.pause(self)

    async def unsubscribe(self) -> None:
        await self._runtime.unsubscribe(self)
