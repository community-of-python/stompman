from dataclasses import dataclass, field
from typing import Any, Self, cast

import stompman
from faststream import AckPolicy, BatchPublishCommand, PublishCommand, PublishType, StreamMessage
from faststream._internal.basic_types import SendableMessage
from faststream._internal.configs import (
    BrokerConfig,
    PublisherSpecificationConfig,
    PublisherUsecaseConfig,
    SubscriberSpecificationConfig,
    SubscriberUsecaseConfig,
)
from faststream._internal.types import AsyncCallable
from faststream._internal.utils.functions import to_async
from faststream.message import decode_message, gen_cor_id
from stompman.core.runtime import Runtime


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
            reply_to=cast("str", message.headers.get("reply-to", "")),
        )


class StompPublishCommand(BatchPublishCommand):
    def __init__(
        self,
        body: SendableMessage,
        /,
        *bodies: SendableMessage,
        _publish_type: PublishType,
        reply_to: str = "",
        destination: str = "",
        correlation_id: str | None = None,
        headers: dict[str, Any] | None = None,
        add_content_length: bool | None = None,
    ) -> None:
        super().__init__(
            body,
            *bodies,
            _publish_type=_publish_type,
            reply_to=reply_to,
            destination=destination,
            correlation_id=correlation_id,
            headers=headers,
        )
        self.add_content_length = add_content_length

    @classmethod
    def from_cmd(
        cls,
        cmd: PublishCommand,
        *,
        batch: bool = False,  # ruff: ignore[unused-class-method-argument]
        add_content_length: bool | None = None,
    ) -> Self:
        messages = cmd.batch_bodies
        if isinstance(cmd, StompPublishCommand) and cmd.add_content_length is not None:
            add_content_length = cmd.add_content_length
        return cls(
            *messages,
            _publish_type=cmd.publish_type,
            reply_to=cmd.reply_to,
            destination=cmd.destination,
            correlation_id=cmd.correlation_id,
            headers=cmd.headers,
            add_content_length=add_content_length,
        )


@dataclass(kw_only=True)
class BrokerConfigWithStompClient(BrokerConfig):
    client: stompman.Client | Runtime


@dataclass(kw_only=True)
class _StompBaseSubscriberConfig:
    destination_without_prefix: str
    ack_mode: stompman.AckMode
    headers: dict[str, str] | None


@dataclass(kw_only=True)
class StompSubscriberSpecificationConfig(_StompBaseSubscriberConfig, SubscriberSpecificationConfig):
    parser: AsyncCallable = StompStreamMessage.from_frame
    decoder: AsyncCallable = field(default=to_async(decode_message))


@dataclass(kw_only=True)
class StompSubscriberUsecaseConfig(_StompBaseSubscriberConfig, SubscriberUsecaseConfig):
    _outer_config: BrokerConfigWithStompClient
    reply_add_content_length: bool | None
    parser: AsyncCallable = StompStreamMessage.from_frame
    decoder: AsyncCallable = field(default=to_async(decode_message))

    @property
    def ack_policy(self) -> AckPolicy:
        return AckPolicy.MANUAL if self.ack_mode == "auto" else AckPolicy.NACK_ON_ERROR

    @property
    def full_destination(self) -> str:
        return self._outer_config.prefix + self.destination_without_prefix


@dataclass(kw_only=True)
class _StompBasePublisherConfig:
    destination_without_prefix: str


@dataclass(kw_only=True)
class StompPublisherSpecificationConfig(_StompBasePublisherConfig, PublisherSpecificationConfig): ...


@dataclass(kw_only=True)
class StompPublisherUsecaseConfig(_StompBasePublisherConfig, PublisherUsecaseConfig):
    _outer_config: BrokerConfigWithStompClient
    add_content_length: bool | None

    @property
    def full_destination(self) -> str:
        return self._outer_config.prefix + self.destination_without_prefix
