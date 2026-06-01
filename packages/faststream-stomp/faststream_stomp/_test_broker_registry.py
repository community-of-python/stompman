import functools
from typing import Any

import faststream.asgi.factories.asyncapi.try_it_out
from faststream._internal.broker import BrokerUsecase
from faststream._internal.testing.broker import TestBroker

from faststream_stomp.broker import StompBroker
from faststream_stomp.testing import TestStompBroker

original_get_broker_registry = faststream.asgi.factories.asyncapi.try_it_out._get_broker_registry


@functools.lru_cache(maxsize=1)
def get_broker_registry() -> dict[type[BrokerUsecase[Any, Any]], type[TestBroker[Any]]]:
    return {**original_get_broker_registry(), StompBroker: TestStompBroker}


def patch_test_broker_registry() -> None:
    faststream.asgi.factories.asyncapi.try_it_out._get_broker_registry = get_broker_registry
