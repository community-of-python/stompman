from collections.abc import Iterable, Sequence
from typing import Any

import stompman
from fast_depends.dependencies import Dependant
from faststream._internal.broker.registrator import Registrator
from faststream._internal.endpoint.subscriber.call_item import CallsCollection
from faststream._internal.types import CustomCallable, PublisherMiddleware, SubscriberMiddleware
from typing_extensions import override

from faststream_stomp.models import (
    BrokerConfigWithStompClient,
    StompPublishCommand,
    StompPublisherSpecificationConfig,
    StompPublisherUsecaseConfig,
    StompSubscriberSpecificationConfig,
    StompSubscriberUsecaseConfig,
)
from faststream_stomp.publisher import StompPublisher, StompPublisherSpecification
from faststream_stomp.subscriber import StompSubscriber, StompSubscriberSpecification


class StompRegistrator(Registrator[stompman.MessageFrame, BrokerConfigWithStompClient]):
    @override
    def subscriber(  # type: ignore[override]
        self,
        destination: str,
        *,
        ack_mode: stompman.AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        # other args
        dependencies: Iterable[Dependant] = (),
        parser: CustomCallable | None = None,
        decoder: CustomCallable | None = None,
        middlewares: Sequence[SubscriberMiddleware[stompman.MessageFrame]] = (),
        title: str | None = None,
        description: str | None = None,
        include_in_schema: bool = True,
    ) -> StompSubscriber:
        usecase_config = StompSubscriberUsecaseConfig(
            _outer_config=self.config,  # type: ignore[arg-type]
            destination_without_prefix=destination,
            ack_mode=ack_mode,
            headers=headers,
        )
        calls = CallsCollection[stompman.MessageFrame]()
        specification = StompSubscriberSpecification(
            _outer_config=self.config,  # type: ignore[arg-type]
            specification_config=StompSubscriberSpecificationConfig(
                title_=title,
                description_=description,
                include_in_schema=include_in_schema,
                destination_without_prefix=destination,
                ack_mode=ack_mode,
                headers=headers,
            ),
            calls=calls,
        )
        subscriber = StompSubscriber(config=usecase_config, specification=specification, calls=calls)
        super().subscriber(subscriber)
        return subscriber.add_call(
            parser_=parser or self._parser,
            decoder_=decoder or self._decoder,
            dependencies_=dependencies,
            middlewares_=middlewares,
        )

    @override
    def publisher(  # type: ignore[override]
        self,
        destination: str,
        *,
        middlewares: Sequence[PublisherMiddleware[StompPublishCommand]] = (),
        schema_: Any | None = None,
        title_: str | None = None,
        description_: str | None = None,
        include_in_schema: bool = True,
    ) -> StompPublisher:
        usecase_config = StompPublisherUsecaseConfig(
            _outer_config=self.config,  # type: ignore[arg-type]
            middlewares=middlewares,
            destination_without_prefix=destination,
        )
        specification = StompPublisherSpecification(
            _outer_config=self.config,  # type: ignore[arg-type]
            specification_config=StompPublisherSpecificationConfig(
                title_=title_,
                description_=description_,
                schema_=schema_,
                include_in_schema=include_in_schema,
                destination_without_prefix=destination,
            ),
        )
        publisher = StompPublisher(config=usecase_config, specification=specification)
        super().publisher(publisher)
        return publisher
