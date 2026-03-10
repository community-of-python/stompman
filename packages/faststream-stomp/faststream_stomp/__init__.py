from faststream_stomp.broker import StompBroker
from faststream_stomp.models import StompPublishCommand, StompStreamMessage
from faststream_stomp.publisher import StompPublisher
from faststream_stomp.router import StompRoute, StompRoutePublisher, StompRouter
from faststream_stomp.subscriber import StompSubscriber
from faststream_stomp.testing import TestStompBroker

__all__ = [
    "StompBroker",
    "StompPublishCommand",
    "StompPublisher",
    "StompRoute",
    "StompRoutePublisher",
    "StompRouter",
    "StompStreamMessage",
    "StompSubscriber",
    "TestStompBroker",
]

try:  # noqa: RUF067
    import functools
    import typing

    import faststream.asgi.factories.asyncapi.try_it_out
    from faststream._internal.broker import BrokerUsecase
    from faststream._internal.testing.broker import TestBroker

    original_get_broker_registry = faststream.asgi.factories.asyncapi.try_it_out._get_broker_registry

    @functools.lru_cache(maxsize=1)
    def get_broker_registry() -> dict[type[BrokerUsecase[typing.Any, typing.Any]], type[TestBroker[typing.Any]]]:
        return {**original_get_broker_registry(), StompBroker: TestStompBroker}

    faststream.asgi.factories.asyncapi.try_it_out._get_broker_registry = get_broker_registry
except Exception:  # noqa: BLE001, S110
    pass
