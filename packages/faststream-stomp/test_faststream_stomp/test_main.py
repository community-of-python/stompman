from unittest import mock

import faker
import faststream_stomp
import pytest
import stompman
from faststream import FastStream
from faststream.asyncapi import get_app_schema
from faststream.broker.message import gen_cor_id
from faststream_stomp.opentelemetry import StompTelemetryMiddleware
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from test_stompman.conftest import build_dataclass

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake_connection_params() -> stompman.ConnectionParameters:
    return build_dataclass(stompman.ConnectionParameters)


@pytest.fixture
def broker(fake_connection_params: stompman.ConnectionParameters) -> faststream_stomp.StompBroker:
    return faststream_stomp.StompBroker(stompman.Client([fake_connection_params]))


async def test_testing(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    expected_body, first_destination, second_destination, third_destination, correlation_id = (
        faker.pystr(),
        faker.pystr(),
        faker.pystr(),
        faker.pystr(),
        gen_cor_id(),
    )
    second_publisher = broker.publisher(second_destination)
    third_publisher = broker.publisher(third_destination)

    @broker.subscriber(first_destination)
    @second_publisher
    @third_publisher
    def first_handle(body: str) -> str:
        assert body == expected_body
        return body

    @broker.subscriber(second_destination)
    def second_handle(body: str) -> None:
        assert body == expected_body

    async with faststream_stomp.TestStompBroker(broker) as br:
        await br.publish(expected_body, first_destination, correlation_id=correlation_id)
        assert first_handle.mock
        first_handle.mock.assert_called_once_with(expected_body)
        assert second_publisher.mock
        second_publisher.mock.assert_called_once_with(expected_body)
        assert third_publisher.mock
        third_publisher.mock.assert_called_once_with(expected_body)


async def test_broker_request_not_implemented(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    async with faststream_stomp.TestStompBroker(broker):
        with pytest.raises(NotImplementedError):
            await broker.request(faker.pystr())


async def test_publisher_request_not_implemented(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    async with faststream_stomp.TestStompBroker(broker):
        with pytest.raises(NotImplementedError):
            await broker.publisher(faker.pystr()).request(faker.pystr())


def test_get_fmt(broker: faststream_stomp.StompBroker) -> None:
    broker.get_fmt()


def test_asyncapi_schema(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    broker.include_router(
        faststream_stomp.StompRouter(
            handlers=(
                faststream_stomp.StompRoute(
                    mock.Mock(), faker.pystr(), publishers=(faststream_stomp.StompRoutePublisher(faker.pystr()),)
                ),
            )
        )
    )
    get_app_schema(FastStream(broker))


async def test_opentelemetry_publish(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    broker.add_middleware(StompTelemetryMiddleware(tracer_provider=TracerProvider(), meter_provider=MeterProvider()))

    @broker.subscriber(destination := faker.pystr())
    def _() -> None: ...

    async with faststream_stomp.TestStompBroker(broker):
        await broker.start()
        await broker.publish(faker.pystr(), destination, correlation_id=gen_cor_id())
