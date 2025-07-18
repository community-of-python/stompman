from collections.abc import Iterable, Sequence
from typing import Any, cast

import stompman
from fast_depends.dependencies import Depends
from faststream.broker.core.abc import ABCBroker
from faststream.broker.types import CustomCallable, PublisherMiddleware, SubscriberMiddleware
from faststream.broker.utils import default_filter
from typing_extensions import override

from faststream_stomp.publisher import StompPublisher
from faststream_stomp.subscriber import StompSubscriber


class StompRegistrator(ABCBroker[stompman.MessageFrame]):
    @override
    def subscriber(  # type: ignore[override]
        self,
        destination: str,
        *,
        ack_mode: stompman.AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        # other args
        dependencies: Iterable[Depends] = (),
        no_ack: bool = False,
        parser: CustomCallable | None = None,
        decoder: CustomCallable | None = None,
        middlewares: Sequence[SubscriberMiddleware[stompman.MessageFrame]] = (),
        retry: bool = False,
        title: str | None = None,
        description: str | None = None,
        include_in_schema: bool = True,
    ) -> StompSubscriber:
        subscriber = cast(
            "StompSubscriber",
            super().subscriber(
                StompSubscriber(
                    destination=destination,
                    ack_mode=ack_mode,
                    headers=headers,
                    retry=retry,
                    no_ack=no_ack,
                    broker_middlewares=self._middlewares,
                    broker_dependencies=self._dependencies,
                    title_=title,
                    description_=description,
                    include_in_schema=self._solve_include_in_schema(include_in_schema),
                )
            ),
        )
        return subscriber.add_call(
            filter_=default_filter,
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
