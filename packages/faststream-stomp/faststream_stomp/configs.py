from dataclasses import dataclass, field

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
from faststream.message import decode_message
from faststream.middlewares import AckPolicy

from faststream_stomp.message import StompStreamMessage


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

    @property
    def ack_policy(self) -> AckPolicy:
        return AckPolicy.MANUAL if self.ack_mode == "auto" else AckPolicy.NACK_ON_ERROR


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
