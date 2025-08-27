import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any, NoReturn

import stompman
from faststream import PublishCommand, StreamMessage
from faststream._internal.configs import BrokerConfig
from faststream._internal.endpoint.publisher.fake import FakePublisher
from faststream._internal.endpoint.subscriber import SubscriberSpecification, SubscriberUsecase
from faststream._internal.endpoint.subscriber.call_item import CallsCollection
from faststream._internal.producer import ProducerProto
from faststream.specification.asyncapi.utils import resolve_payloads
from faststream.specification.schema import Message, Operation, SubscriberSpec

from faststream_stomp.models import (
    StompPublishCommand,
    StompSubscriberSpecificationConfig,
    StompSubscriberUsecaseConfig,
)


class StompSubscriberSpecification(SubscriberSpecification[BrokerConfig, StompSubscriberSpecificationConfig]):
    @property
    def name(self) -> str:
        return f"{self._outer_config.prefix}{self.config.destination_without_prefix}:{self.call_name}"

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


class StompFakePublisher(FakePublisher):
    def __init__(self, *, producer: ProducerProto[Any], reply_to: str) -> None:
        super().__init__(producer=producer)
        self.reply_to = reply_to

    def patch_command(self, cmd: PublishCommand | StompPublishCommand) -> StompPublishCommand:
        cmd = super().patch_command(cmd)
        real_cmd = StompPublishCommand.from_cmd(cmd)
        real_cmd.destination = self.reply_to
        return real_cmd


class StompSubscriber(SubscriberUsecase[stompman.MessageFrame]):
    def __init__(
        self,
        *,
        config: StompSubscriberUsecaseConfig,
        specification: StompSubscriberSpecification,
        calls: CallsCollection[stompman.MessageFrame],
    ) -> None:
        self.config = config
        self._subscription: stompman.ManualAckSubscription | None = None
        super().__init__(config=config, specification=specification, calls=calls)  # type: ignore[arg-type]

    async def start(self) -> None:
        await super().start()
        self._subscription = await self.config._outer_config.client.subscribe_with_manual_ack(
            destination=self.config.full_destination,
            handler=self.consume,
            ack=self.config.ack_mode,
            headers=self.config.headers,
        )
        self._post_start()

    async def stop(self) -> None:
        if self._subscription:
            await self._subscription.unsubscribe()
        await super().stop()

    async def get_one(self, *, timeout: float = 5) -> NoReturn:
        raise NotImplementedError

    async def __aiter__(self) -> AsyncIterator[StreamMessage[stompman.MessageFrame]]:  # type: ignore[override, misc]
        raise NotImplementedError
        yield  # pragma: no cover
        await asyncio.sleep(0)  # pragma: no cover

    def _make_response_publisher(self, message: StreamMessage[stompman.MessageFrame]) -> Sequence[FakePublisher]:
        return (StompFakePublisher(producer=self.config._outer_config.producer, reply_to=message.reply_to),)

    def get_log_context(self, message: StreamMessage[stompman.MessageFrame] | None) -> dict[str, str]:
        return {
            "destination": message.raw_message.headers["destination"] if message else self.config.full_destination,
            "message_id": message.message_id if message else "",
        }
