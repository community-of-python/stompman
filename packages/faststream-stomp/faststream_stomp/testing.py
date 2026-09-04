import copy
import typing
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest import mock
from unittest.mock import AsyncMock

import stompman
from faststream._internal.parser import DefaultCodec
from faststream._internal.testing.broker import TestBroker, change_producer
from faststream.message import encode_message
from stompman.frames import SendFrame
from stompman.serde import dump_frame

from faststream_stomp.broker import StompBroker
from faststream_stomp.models import StompPublishCommand
from faststream_stomp.publisher import StompProducer, StompPublisher, _make_headers_for_publish
from faststream_stomp.subscriber import StompSubscriber

if TYPE_CHECKING:
    from stompman.frames import MessageHeaders


class TestStompBroker(TestBroker[StompBroker]):
    async def __aenter__(self) -> StompBroker:
        return typing.cast("StompBroker", await super().__aenter__())

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
    def _patch_producer(self, broker: StompBroker) -> Iterator[None]:  # ruff: ignore[no-self-use]
        with change_producer(broker.config.broker_config, FakeStompProducer(broker)):
            yield

    @contextmanager
    def _patch_broker(self, broker: StompBroker) -> Generator[None, None, None]:
        with mock.patch.object(broker.config, "client", new_callable=AsyncMock), super()._patch_broker(broker):
            yield

    async def _fake_connect(self, broker: StompBroker, *args: Any, **kwargs: Any) -> None: ...  # ruff: ignore[any-type]


class FakeAckableMessageFrame(stompman.AckableMessageFrame):
    async def ack(self) -> None: ...

    async def nack(self) -> None: ...


class FakeStompProducer(StompProducer):
    def __init__(self, broker: StompBroker) -> None:
        self.broker = broker
        self.add_content_length = typing.cast("StompProducer", broker.config.producer).add_content_length
        self.codec = DefaultCodec()

    async def publish(self, cmd: StompPublishCommand) -> None:
        body, content_type = encode_message(cmd.body, serializer=self.broker.config.fd_config._serializer)
        send_frame = SendFrame.build(
            body=body,
            destination=cmd.destination,
            transaction=None,
            content_type=content_type,
            add_content_length=self._resolve_add_content_length(cmd),
            headers=_make_headers_for_publish(cmd),
        )
        dump_frame(send_frame)
        all_headers: MessageHeaders = send_frame.headers | {  # type: ignore[assignment]
            "destination": cmd.destination,
            "message-id": str(uuid.uuid4()),
            "subscription": str(uuid.uuid4()),
        }
        frame = FakeAckableMessageFrame(
            headers=all_headers, body=body, _subscription=mock.AsyncMock(), _generation=0, _sequence=0
        )
        for handler in self.broker.subscribers:
            if typing.cast("StompSubscriber", handler).config.full_destination == cmd.destination:
                await handler.process_message(frame)

    async def publish_batch(self, cmd: StompPublishCommand) -> None:
        for one_body in cmd.batch_bodies:
            new_cmd = copy.deepcopy(cmd)
            new_cmd.body = one_body
            new_cmd.extra_bodies = ()
            await self.publish(new_cmd)
