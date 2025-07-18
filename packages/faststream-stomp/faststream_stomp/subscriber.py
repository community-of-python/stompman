from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import stompman
from faststream._internal.endpoint.publisher.fake import FakePublisher
from faststream._internal.endpoint.subscriber import SubscriberSpecification, SubscriberUsecase
from faststream._internal.endpoint.subscriber.call_item import CallsCollection
from faststream._internal.producer import ProducerProto
from faststream.message import StreamMessage
from faststream.rabbit.response import RabbitPublishCommand
from faststream.response.response import PublishCommand
from faststream.specification.asyncapi.utils import resolve_payloads
from faststream.specification.schema import (
    Message,
    Operation,
    SubscriberSpec,
)

from faststream_stomp.configs import StompBrokerConfig, StompSubscriberSpecificationConfig, StompSubscriberUsecaseConfig
from faststream_stomp.publisher import StompPublishCommand


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


class StompFakePublisher(FakePublisher):
    def __init__(self, *, producer: ProducerProto[Any], reply_to: str) -> None:
        super().__init__(producer=producer)
        self.reply_to = reply_to

    def patch_command(self, cmd: PublishCommand | StompPublishCommand) -> "RabbitPublishCommand":
        cmd = super().patch_command(cmd)
        real_cmd = RabbitPublishCommand.from_cmd(cmd)
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
        super().__init__(config=config, specification=cast("SubscriberSpecification", specification), calls=calls)

    async def start(self) -> None:
        await super().start()
        self._subscription = await self.config._outer_config.client.subscribe_with_manual_ack(  # noqa: SLF001
            destination=self.config._outer_config.prefix + self.config.destination,  # noqa: SLF001
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
            (StompFakePublisher(producer=self.config._outer_config.producer, reply_to=message.reply_to),)  # noqa: SLF001
        )

    def get_log_context(self, message: StreamMessage[stompman.MessageFrame] | None) -> dict[str, str]:
        return {
            "destination": message.raw_message.headers["destination"]
            if message
            else self.config._outer_config.prefix + self.config.destination,  # noqa: SLF001
            "message_id": message.message_id if message else "",
        }
