"""Keep explicit legacy injection separate from native FastStream execution."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, cast

import stompman
from stompman.core.delivery import Delivery
from stompman.core.runtime import Runtime
from stompman.core.subscriptions import Subscription

MessageHandler = Callable[[stompman.AckableMessageFrame], Coroutine[Any, Any, Any]]


class SubscriptionAdapter(Protocol):
    def pause(self) -> None: ...

    async def unsubscribe(self) -> None: ...


@dataclass(frozen=True)
class NativeClientAdapter:
    client: Runtime

    async def open(self) -> None:
        await self.client.start()

    async def close(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.client.close(exc_type, exc_value, traceback, cancel_handlers=True)

    async def subscribe(
        self, destination: str, handler: MessageHandler, *, ack: stompman.AckMode, headers: dict[str, str] | None
    ) -> Subscription:
        async def consume(delivery: Delivery) -> None:
            await handler(stompman.AckableMessageFrame.from_delivery(delivery))

        return await self.client.subscribe(destination, consume, ack=ack, headers=headers)


class LegacyConsumer:
    """Own forwarding and graceful-stop cancellation for an injected client."""

    def __init__(self, handler: MessageHandler) -> None:
        self.handler = handler
        self.accepting = True
        self.tasks: set[asyncio.Task[Any]] = set()

    async def consume(self, frame: stompman.AckableMessageFrame) -> None:
        if not self.accepting:
            return
        task = cast("asyncio.Task[Any]", asyncio.current_task())
        self.tasks.add(task)
        try:
            await self.handler(frame)
        finally:
            self.tasks.remove(task)

    def pause(self) -> None:
        self.accepting = False

    async def cancel_remaining(self) -> None:
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@dataclass(frozen=True)
class LegacySubscriptionAdapter:
    subscription: stompman.ManualAckSubscription
    consumer: LegacyConsumer

    def pause(self) -> None:
        self.consumer.pause()

    async def unsubscribe(self) -> None:
        try:
            await self.consumer.cancel_remaining()
        finally:
            await self.subscription.unsubscribe()


@dataclass(frozen=True)
class LegacyClientAdapter:
    client: stompman.Client

    async def open(self) -> None:
        await self.client.__aenter__()

    async def close(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.client.__aexit__(exc_type, exc_value, traceback)

    async def subscribe(
        self, destination: str, handler: MessageHandler, *, ack: stompman.AckMode, headers: dict[str, str] | None
    ) -> LegacySubscriptionAdapter:
        consumer = LegacyConsumer(handler)
        subscription = await self.client.subscribe_with_manual_ack(
            destination, consumer.consume, ack=ack, headers=headers
        )
        return LegacySubscriptionAdapter(subscription, consumer)


def adapt_client(client: stompman.Client | Runtime) -> NativeClientAdapter | LegacyClientAdapter:
    if isinstance(client, Runtime):
        return NativeClientAdapter(client)
    return LegacyClientAdapter(client)
