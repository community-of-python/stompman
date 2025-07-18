from collections.abc import Iterable, Sequence
from typing import Any, cast

import stompman
from fast_depends.dependencies import Depends
from faststream._internal.broker.registrator import Registrator
from faststream._internal.endpoint.subscriber.call_item import CallsCollection
from faststream._internal.types import CustomCallable, PublisherMiddleware
from typing_extensions import override

from faststream_stomp.configs import StompBrokerConfig, StompSubscriberSpecificationConfig, StompSubscriberUsecaseConfig
from faststream_stomp.publisher import StompPublisher
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
        usecase_config = StompSubscriberUsecaseConfig(
            _outer_config=self.config.broker_config, destination=destination, ack_mode=ack_mode, headers=headers
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
        middlewares: Sequence[PublisherMiddleware] = (),
        schema_: Any | None = None,
        title_: str | None = None,
        description_: str | None = None,
        include_in_schema: bool = True,
    ) -> StompPublisher:
        return cast(
            "StompPublisher",
            super().publisher(
                StompPublisher(
                    destination,
                    broker_middlewares=self._middlewares,
                    middlewares=middlewares,
                    schema_=schema_,
                    title_=title_,
                    description_=description_,
                    include_in_schema=include_in_schema,
                )
            ),
        )
