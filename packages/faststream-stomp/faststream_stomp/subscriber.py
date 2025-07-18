from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypedDict, cast

import stompman
from fast_depends.dependencies import Dependant
from faststream._internal.basic_types import AnyDict, Decorator, LoggerProto
from faststream._internal.endpoint.publisher.fake import FakePublisher
from faststream._internal.endpoint.subscriber import SubscriberSpecification, SubscriberUsecase
from faststream._internal.producer import ProducerProto
from faststream._internal.types import AsyncCallable, BrokerMiddleware, CustomCallable
from faststream._internal.utils.functions import to_async
from faststream.message import StreamMessage, decode_message
from faststream.specification.asyncapi.utils import resolve_payloads
from faststream.specification.schema import (
    Message,
    Operation,
    SubscriberSpec,
)

from faststream_stomp.broker import StompBrokerConfig
from faststream_stomp.configs import StompSubscriberConfig, StompSubscriberSpecificationConfig
from faststream_stomp.message import StompStreamMessage


class StompLogContext(TypedDict):
    destination: str
    message_id: str


class StompSubscriber(SubscriberUsecase[stompman.MessageFrame]):
    def __init__(
        self,
        *,
        config: StompSubscriberConfig,
        # specification: "SubscriberSpecification",
        # calls: "CallsCollection[stompman.MessageFrame]",
        retry: bool | int,
        no_ack: bool,
        broker_dependencies: Iterable[Dependant],
        broker_middlewares: Sequence[BrokerMiddleware[stompman.MessageFrame]],
        default_parser: AsyncCallable = StompStreamMessage.from_frame,
        default_decoder: AsyncCallable = to_async(decode_message),  # noqa: B008
        # AsyncAPI information
        title_: str | None,
        description_: str | None,
        include_in_schema: bool,
    ) -> None:
        self.destination = destination
        self.ack_mode = ack_mode
        self.headers = headers
        self._subscription: stompman.ManualAckSubscription | None = None

        super().__init__(
            no_ack=no_ack or self.ack_mode == "auto",
            no_reply=True,
            retry=retry,
            broker_dependencies=broker_dependencies,
            broker_middlewares=broker_middlewares,
            default_parser=default_parser,
            default_decoder=default_decoder,
            title_=title_,
            description_=description_,
            include_in_schema=include_in_schema,
        )

    def setup(  # type: ignore[override]
        self,
        client: stompman.Client,
        *,
        logger: LoggerProto | None,
        producer: ProducerProto | None,
        graceful_timeout: float | None,
        extra_context: AnyDict,
        broker_parser: CustomCallable | None,
        broker_decoder: CustomCallable | None,
        apply_types: bool,
        is_validate: bool,
        _get_dependant: Callable[..., Any] | None,
        _call_decorators: Iterable[Decorator],
    ) -> None:
        self.client = client
        return super().setup(
            logger=logger,
            producer=producer,
            graceful_timeout=graceful_timeout,
            extra_context=extra_context,
            broker_parser=broker_parser,
            broker_decoder=broker_decoder,
            apply_types=apply_types,
            is_validate=is_validate,
            _get_dependant=_get_dependant,
            _call_decorators=_call_decorators,
        )

    async def start(self) -> None:
        await super().start()
        self._subscription = await self.client.subscribe_with_manual_ack(
            destination=self.destination,
            handler=self.consume,
            ack=self.ack_mode,
            headers=self.headers,
        )

    async def stop(self) -> None:
        if self._subscription:
            await self._subscription.unsubscribe()
        await super().stop()

    async def get_one(self, *, timeout: float = 5) -> None: ...

    def _make_response_publisher(self, message: StreamMessage[stompman.MessageFrame]) -> Sequence[FakePublisher]:
        return (  # pragma: no cover
            (FakePublisher(self._producer.publish, publish_kwargs={"destination": message.reply_to}),)
            if self._producer
            else ()
        )

    def __hash__(self) -> int:
        return hash(self.destination)

    def get_log_context(self, message: StreamMessage[stompman.MessageFrame] | None) -> dict[str, str]:
        log_context: StompLogContext = {
            "destination": message.raw_message.headers["destination"] if message else self.destination,
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
