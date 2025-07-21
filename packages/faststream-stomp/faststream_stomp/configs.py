from dataclasses import dataclass, field
from typing import Self, cast

import stompman
from faststream._internal.configs import (
    BrokerConfig,
    PublisherSpecificationConfig,
    PublisherUsecaseConfig,
    SubscriberSpecificationConfig,
    SubscriberUsecaseConfig,
)
from faststream._internal.types import AsyncCallable
from faststream._internal.utils.functions import to_async
from faststream.message import StreamMessage, decode_message, gen_cor_id
from faststream.middlewares import AckPolicy
from faststream.response.response import PublishCommand


class StompStreamMessage(StreamMessage[stompman.AckableMessageFrame]):
    async def ack(self) -> None:
        if not self.committed:
            await self.raw_message.ack()
        return await super().ack()

    async def nack(self) -> None:
        if not self.committed:
            await self.raw_message.nack()
        return await super().nack()

    async def reject(self) -> None:
        if not self.committed:
            await self.raw_message.nack()
        return await super().reject()

    @classmethod
    async def from_frame(cls, message: stompman.AckableMessageFrame) -> Self:
        return cls(
            raw_message=message,
            body=message.body,
            headers=cast("dict[str, str]", message.headers),
            content_type=message.headers.get("content-type"),
            message_id=message.headers["message-id"],
            correlation_id=cast("str", message.headers.get("correlation-id", gen_cor_id())),
        )


class StompPublishCommand(PublishCommand):
    @classmethod
    def from_cmd(cls, cmd: PublishCommand) -> Self:
        return cmd  # type: ignore[return-value]


@dataclass(kw_only=True)
class StompBrokerConfig(BrokerConfig):
    client: stompman.Client


@dataclass(kw_only=True)
class StompBaseSubscriberConfig:
    destination_without_prefix: str
    ack_mode: stompman.AckMode
    headers: dict[str, str] | None


@dataclass(kw_only=True)
class StompSubscriberSpecificationConfig(StompBaseSubscriberConfig, SubscriberSpecificationConfig):
    parser: AsyncCallable = StompStreamMessage.from_frame
    decoder: AsyncCallable = field(default=to_async(decode_message))


@dataclass(kw_only=True)
class StompSubscriberUsecaseConfig(StompBaseSubscriberConfig, SubscriberUsecaseConfig):
    _outer_config: StompBrokerConfig
    parser: AsyncCallable = StompStreamMessage.from_frame
    decoder: AsyncCallable = field(default=to_async(decode_message))

    @property
    def ack_policy(self) -> AckPolicy:
        return AckPolicy.MANUAL if self.ack_mode == "auto" else AckPolicy.NACK_ON_ERROR

    @property
    def full_destination(self) -> str:
        return self._outer_config.prefix + self.destination_without_prefix


@dataclass(kw_only=True)
class StompBasePublisherConfig:
    destination_without_prefix: str


@dataclass(kw_only=True)
class StompPublisherSpecificationConfig(StompBasePublisherConfig, PublisherSpecificationConfig): ...


@dataclass(kw_only=True)
class StompPublisherUsecaseConfig(StompBasePublisherConfig, PublisherUsecaseConfig):
    _outer_config: StompBrokerConfig

    @property
    def full_destination(self) -> str:
        return self._outer_config.prefix + self.destination_without_prefix
