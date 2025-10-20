from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

import stompman
from fast_depends.dependencies import Dependant
from faststream._internal.basic_types import SendableMessage
from faststream._internal.broker.router import ArgsContainer, BrokerRouter, SubscriberRoute
from faststream._internal.configs import BrokerConfig
from faststream._internal.types import BrokerMiddleware, CustomCallable, PublisherMiddleware, SubscriberMiddleware

from faststream_stomp.models import StompPublishCommand
from faststream_stomp.registrator import StompRegistrator


class StompRoutePublisher(ArgsContainer):
    """Delayed StompPublisher registration object.

    Just a copy of StompRegistrator.publisher(...) arguments.
    """

    def __init__(
        self,
        destination: str,
        *,
        middlewares: Sequence[PublisherMiddleware[StompPublishCommand]] = (),
        schema_: Any | None = None,  # noqa: ANN401
        title_: str | None = None,
        description_: str | None = None,
        include_in_schema: bool = True,
    ) -> None:
        super().__init__(
            destination=destination,
            middlewares=middlewares,
            schema_=schema_,
            title_=title_,
            description_=description_,
            include_in_schema=include_in_schema,
        )


class StompRoute(SubscriberRoute):
    """Class to store delayed StompBroker subscriber registration.

    Just a copy of StompRegistrator.subscriber(...) arguments + `call` and `publishers` argument.
    """

    def __init__(
        self,
        call: Callable[..., SendableMessage] | Callable[..., Awaitable[SendableMessage]],
        destination: str,
        *,
        ack_mode: stompman.AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        # other args
        publishers: Iterable[StompRoutePublisher] = (),
        dependencies: Iterable[Dependant] = (),
        parser: CustomCallable | None = None,
        decoder: CustomCallable | None = None,
        middlewares: Sequence[SubscriberMiddleware[stompman.MessageFrame]] = (),
        title: str | None = None,
        description: str | None = None,
        include_in_schema: bool = True,
    ) -> None:
        super().__init__(
            call=call,
            destination=destination,
            ack_mode=ack_mode,
            headers=headers,
            publishers=publishers,
            dependencies=dependencies,
            parser=parser,
            decoder=decoder,
            middlewares=middlewares,
            title=title,
            description=description,
            include_in_schema=include_in_schema,
        )


class StompRouter(StompRegistrator, BrokerRouter[stompman.MessageFrame, BrokerConfig]):
    """Includable to StompBroker router."""

    def __init__(
        self,
        prefix: str = "",
        handlers: Iterable[StompRoute] = (),
        *,
        dependencies: Iterable[Dependant] = (),
        middlewares: Sequence[BrokerMiddleware[stompman.MessageFrame]] = (),
        parser: CustomCallable | None = None,
        decoder: CustomCallable | None = None,
        include_in_schema: bool | None = None,
        routers: Sequence[StompRegistrator] = (),
    ) -> None:
        super().__init__(
            config=BrokerConfig(
                broker_middlewares=middlewares,
                broker_dependencies=dependencies,
                broker_parser=parser,
                broker_decoder=decoder,
                include_in_schema=include_in_schema,
                prefix=prefix,
            ),
            handlers=handlers,
            routers=routers,
        )
