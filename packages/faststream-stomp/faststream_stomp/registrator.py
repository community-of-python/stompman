from collections.abc import Iterable
from typing import Any

import stompman
from fast_depends.dependencies import Depends
from faststream._internal.broker.registrator import Registrator
from faststream._internal.endpoint.subscriber.call_item import CallsCollection
from faststream._internal.types import CustomCallable
from typing_extensions import override

from faststream_stomp.configs import (
    StompBrokerConfig,
    StompPublisherSpecificationConfig,
    StompPublisherUsecaseConfig,
    StompSubscriberSpecificationConfig,
    StompSubscriberUsecaseConfig,
)
from faststream_stomp.publisher import StompPublisher, StompPublisherSpecification
from faststream_stomp.subscriber import StompSubscriber, StompSubscriberSpecification


class StompRegistrator(Registrator[stompman.MessageFrame, StompBrokerConfig]):
    @override
    def subscriber(  # type: ignore[override]
        self,
        destination: str,
        *,
        ack_mode: stompman.AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        # other args
        dependencies: Iterable[Depends] = (),
        parser: CustomCallable | None = None,
        decoder: CustomCallable | None = None,
        title: str | None = None,
        description: str | None = None,
        include_in_schema: bool = True,
    ) -> StompSubscriber:
        usecase_config = StompSubscriberUsecaseConfig(
            _outer_config=self.config.broker_config, destination=destination, ack_mode=ack_mode, headers=headers
        )
        calls = CallsCollection[stompman.MessageFrame]()
        specification = StompSubscriberSpecification(
            _outer_config=self.config.broker_config,
            specification_config=StompSubscriberSpecificationConfig(
                title_=title,
                description_=description,
                include_in_schema=include_in_schema,
                destination=destination,
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
            middlewares_=(),
            dependencies_=dependencies,
        )

    @override
    def publisher(  # type: ignore[override]
        self,
        destination: str,
        *,
        title: str | None = None,
        description: str | None = None,
        schema: Any | None = None,
        include_in_schema: bool = True,
    ) -> StompPublisher:
        usecase_config = StompPublisherUsecaseConfig(
            _outer_config=self.config.broker_config, middlewares=(), destination=destination
        )
        specification = StompPublisherSpecification(
            _outer_config=self.config.broker_config,
            specification_config=StompPublisherSpecificationConfig(
                title_=title,
                description_=description,
                schema_=schema,
                include_in_schema=include_in_schema,
                destination=destination,
            ),
        )
        publisher = StompPublisher(config=usecase_config, specification=specification)
        super().publisher(publisher)
        return publisher
