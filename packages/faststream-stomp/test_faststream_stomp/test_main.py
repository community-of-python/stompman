import faker
import faststream_stomp
import pydantic
import pytest
import stompman
from faststream import FastStream
from faststream.message import gen_cor_id
from faststream_stomp.opentelemetry import StompTelemetryMiddleware
from faststream_stomp.prometheus import StompPrometheusMiddleware
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from polyfactory.factories.pydantic_factory import ModelFactory
from prometheus_client import CollectorRegistry
from test_stompman.conftest import build_dataclass

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake_connection_params() -> stompman.ConnectionParameters:
    return build_dataclass(stompman.ConnectionParameters)


@pytest.fixture
def broker(fake_connection_params: stompman.ConnectionParameters) -> faststream_stomp.StompBroker:
    return faststream_stomp.StompBroker(stompman.Client([fake_connection_params]))


class SomePydanticModel(pydantic.BaseModel):
    foo: str


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

        await br.publish(ModelFactory.create_factory(SomePydanticModel).build(), faker.pystr())


class TestNotImplemented:
    async def test_broker_request(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        async with faststream_stomp.TestStompBroker(broker):
            with pytest.raises(NotImplementedError):
                await broker.request(faker.pystr(), faker.pystr())

    async def test_broker_publish_batch(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        async with faststream_stomp.TestStompBroker(broker):
            with pytest.raises(NotImplementedError):
                await broker.publish_batch(faker.pystr(), destination=faker.pystr())

    async def test_publisher_request(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        async with faststream_stomp.TestStompBroker(broker):
            with pytest.raises(NotImplementedError):
                await broker.publisher(faker.pystr()).request(faker.pystr())

    async def test_subscriber_get_one(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        async with faststream_stomp.TestStompBroker(broker):
            with pytest.raises(NotImplementedError):
                await broker.subscriber(faker.pystr()).get_one()

    async def test_subscriber_aiter(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        async with faststream_stomp.TestStompBroker(broker):
            with pytest.raises(NotImplementedError):
                async for _ in broker.subscriber(faker.pystr()):
                    ...  # pragma: no cover


def test_asyncapi_schema(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    @broker.publisher(faker.pystr())
    def _publisher() -> None: ...

    @broker.subscriber(faker.pystr())
    def _subscriber() -> None: ...

    FastStream(broker).schema.to_specification()


async def test_opentelemetry_publish(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    broker.add_middleware(StompTelemetryMiddleware(tracer_provider=TracerProvider(), meter_provider=MeterProvider()))

    @broker.subscriber(destination := faker.pystr())
    def _() -> None: ...

    async with faststream_stomp.TestStompBroker(broker):
        await broker.start()
        await broker.publish(faker.pystr(), destination, correlation_id=gen_cor_id())


async def test_prometheus_publish(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    broker.add_middleware(StompPrometheusMiddleware(registry=CollectorRegistry()))

    @broker.subscriber(destination := faker.pystr())
    def _() -> None: ...

    async with faststream_stomp.TestStompBroker(broker):
        await broker.start()
        await broker.publish(faker.pystr(), destination, correlation_id=gen_cor_id())
