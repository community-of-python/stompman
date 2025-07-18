import uuid
from typing import TYPE_CHECKING, Any
from unittest import mock
from unittest.mock import AsyncMock

import stompman
from faststream._internal.basic_types import SendableMessage
from faststream._internal.testing.broker import TestBroker
from faststream.message import encode_message

from faststream_stomp.broker import StompBroker
from faststream_stomp.publisher import StompProducer, StompPublisher
from faststream_stomp.subscriber import StompSubscriber

if TYPE_CHECKING:
    from stompman.frames import MessageHeaders


class TestStompBroker(TestBroker[StompBroker]):
    @staticmethod
    def create_publisher_fake_subscriber(
        broker: StompBroker, publisher: StompPublisher
    ) -> tuple[StompSubscriber, bool]:
        subscriber: StompSubscriber | None = None
        for handler in broker._subscribers.values():  # noqa: SLF001
            if handler.destination == publisher.destination:
                subscriber = handler
                break

        if subscriber is None:
            is_real = False
            subscriber = broker.subscriber(publisher.destination)
        else:
            is_real = True

        return subscriber, is_real

    @staticmethod
    async def _fake_connect(
        broker: StompBroker,
        *args: Any,  # noqa: ANN401, ARG004
        **kwargs: Any,  # noqa: ANN401, ARG004
    ) -> None:
        broker._connection = AsyncMock()  # noqa: SLF001
        broker._producer = FakeStompProducer(broker)  # noqa: SLF001


class FakeAckableMessageFrame(stompman.AckableMessageFrame):
    async def ack(self) -> None: ...

    async def nack(self) -> None: ...


class FakeStompProducer(StompProducer):
    def __init__(self, broker: StompBroker) -> None:
        self.broker = broker

    async def publish(  # type: ignore[override]
        self,
        message: SendableMessage,
        *,
        destination: str,
        correlation_id: str | None,
        headers: dict[str, str] | None,
    ) -> None:
        body, content_type = encode_message(message)
        all_headers: MessageHeaders = (headers.copy() if headers else {}) | {  # type: ignore[assignment]
            "destination": destination,
            "message-id": str(uuid.uuid4()),
            "subscription": str(uuid.uuid4()),
        }
        if correlation_id:
            all_headers["correlation-id"] = correlation_id  # type: ignore[typeddict-unknown-key]
        if content_type:
            all_headers["content-type"] = content_type
        frame = FakeAckableMessageFrame(headers=all_headers, body=body, _subscription=mock.AsyncMock())

        for handler in self.broker._subscribers.values():  # noqa: SLF001
            if handler.destination == destination:
                await handler.process_message(frame)
