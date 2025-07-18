from collections.abc import AsyncIterator, Sequence
from typing import TypedDict, cast

import stompman
from faststream._internal.endpoint.publisher.fake import FakePublisher
from faststream._internal.endpoint.subscriber import SubscriberSpecification, SubscriberUsecase
from faststream._internal.endpoint.subscriber.call_item import CallsCollection
from faststream.message import StreamMessage
from faststream.specification.asyncapi.utils import resolve_payloads
from faststream.specification.schema import (
    Message,
    Operation,
    SubscriberSpec,
)

from faststream_stomp.broker import StompBrokerConfig
from faststream_stomp.configs import StompSubscriberSpecificationConfig, StompSubscriberUsecaseConfig


class StompLogContext(TypedDict):
    destination: str
    message_id: str


class StompSubscriber(SubscriberUsecase[stompman.MessageFrame]):
    def __init__(
        self,
        *,
        config: StompSubscriberUsecaseConfig,
        specification: "SubscriberSpecification",
        calls: CallsCollection[stompman.MessageFrame],
    ) -> None:
        self.config = config
        self._subscription: stompman.ManualAckSubscription | None = None
        super().__init__(config=config, specification=specification, calls=calls)

    async def start(self) -> None:
        await super().start()
        self._subscription = await self.config._outer_config.client.subscribe_with_manual_ack(
            destination=self.config._outer_config.prefix + self.config.destination,
            handler=self.consume,
            ack=self.config.ack_mode,
            headers=self.config.headers,
        )

    async def stop(self) -> None:
        if self._subscription:
            await self._subscription.unsubscribe()
        await super().stop()

    async def get_one(self, *, timeout: float = 5) -> None:
        raise NotImplementedError

    async def __aiter__(self) -> AsyncIterator[StreamMessage[stompman.MessageFrame]]:
        raise NotImplementedError

    def _make_response_publisher(self, message: StreamMessage[stompman.MessageFrame]) -> Sequence[FakePublisher]:
        return (  # pragma: no cover
            (FakePublisher(self._outer_config.producer.publish, publish_kwargs={"destination": message.reply_to}),)
        )

    def get_log_context(self, message: StreamMessage[stompman.MessageFrame] | None) -> dict[str, str]:
        log_context: StompLogContext = {
            "destination": message.raw_message.headers["destination"]
            if message
            else self.config._outer_config.prefix + self.config.destination,
            "message_id": message.message_id if message else "",
        }
        return cast("dict[str, str]", log_context)


class StompSubscriberSpecification(SubscriberSpecification[StompBrokerConfig, StompSubscriberSpecificationConfig]):
    @property
    def name(self) -> str:
        return f"{self._outer_config.prefix}{self.config.destination}:{self.call_name}"

    def get_schema(self) -> dict[str, SubscriberSpec]:
        return {
            self.name: SubscriberSpec(
                description=self.description,
                operation=Operation(
                    message=Message(title=f"{self.name}:Message", payload=resolve_payloads(self.get_payloads())),
                    bindings=None,
                ),
                bindings=None,
            )
        }
