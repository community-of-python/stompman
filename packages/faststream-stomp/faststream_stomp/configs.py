from dataclasses import dataclass, field

import stompman
from faststream._internal.configs import (
    SubscriberSpecificationConfig,
    SubscriberUsecaseConfig,
)
from faststream._internal.types import AsyncCallable
from faststream._internal.utils.functions import to_async
from faststream.message import decode_message
from faststream.middlewares import AckPolicy

from faststream_stomp.broker import StompBrokerConfig
from faststream_stomp.message import StompStreamMessage


@dataclass(kw_only=True)
class StompBaseSubscriberConfig:
    destination: str
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
