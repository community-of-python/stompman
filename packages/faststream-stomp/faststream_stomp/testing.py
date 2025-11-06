import typing
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest import mock
from unittest.mock import AsyncMock

import stompman
from faststream._internal.testing.broker import TestBroker, change_producer
from faststream.message import encode_message

from faststream_stomp.broker import StompBroker
from faststream_stomp.models import StompPublishCommand
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
        for handler in broker._subscribers:
            if handler.config.full_destination == publisher.config.full_destination:
                subscriber = handler
                break
        if subscriber is None:
            is_real = False
            subscriber = broker.subscriber(publisher.config.full_destination)
        else:
            is_real = True

        return subscriber, is_real

    @contextmanager
    def _patch_producer(self, broker: StompBroker) -> Iterator[None]:  # noqa: PLR6301
        with change_producer(broker.config.broker_config, FakeStompProducer(broker)):
            yield

    @contextmanager
    def _patch_broker(self, broker: StompBroker) -> Generator[None, None, None]:
        with mock.patch.object(broker.config, "client", new_callable=AsyncMock), super()._patch_broker(broker):
            yield

    @staticmethod
    async def _fake_connect(broker: StompBroker, *args: Any, **kwargs: Any) -> None: ...  # noqa: ANN401


class FakeAckableMessageFrame(stompman.AckableMessageFrame):
    async def ack(self) -> None: ...

    async def nack(self) -> None: ...


class FakeStompProducer(StompProducer):
    def __init__(self, broker: StompBroker) -> None:
        self.broker = broker

    async def publish(self, cmd: StompPublishCommand) -> None:
        body, content_type = encode_message(cmd.body, serializer=self.broker.config.fd_config._serializer)
        all_headers: MessageHeaders = (cmd.headers.copy() if cmd.headers else {}) | {  # type: ignore[assignment]
            "destination": cmd.destination,
            "message-id": str(uuid.uuid4()),
            "subscription": str(uuid.uuid4()),
        }
        if cmd.correlation_id:
            all_headers["correlation-id"] = cmd.correlation_id  # type: ignore[typeddict-unknown-key]
        if content_type:
            all_headers["content-type"] = content_type
        frame = FakeAckableMessageFrame(headers=all_headers, body=body, _subscription=mock.AsyncMock())
        for handler in self.broker.subscribers:
            if typing.cast("StompSubscriber", handler).config.full_destination == cmd.destination:
                await handler.process_message(frame)
