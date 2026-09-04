import asyncio
import contextlib
import logging
import typing
from unittest import mock

import faker
import faststream_stomp
import pydantic
import pytest
import stompman
from faststream import FastStream, PublishCommand, PublishType
from faststream.message import gen_cor_id
from faststream_stomp.broker import _handle_listen_task_done
from faststream_stomp.opentelemetry import StompTelemetryMiddleware
from faststream_stomp.prometheus import StompPrometheusMiddleware
from faststream_stomp.router import StompRouter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from polyfactory.factories.pydantic_factory import ModelFactory
from prometheus_client import CollectorRegistry
from test_stompman.conftest import build_dataclass

if typing.TYPE_CHECKING:
    from faststream_stomp.subscriber import StompFakePublisher

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake_connection_params() -> stompman.ConnectionParameters:
    return build_dataclass(stompman.ConnectionParameters)


@pytest.fixture
def broker(fake_connection_params: stompman.ConnectionParameters) -> faststream_stomp.StompBroker:
    return faststream_stomp.StompBroker(stompman.Client([fake_connection_params]))


def make_mock_client(
    connection_parameters: stompman.ConnectionParameters,
) -> tuple[stompman.Client, mock.NonCallableMagicMock]:
    client_mock = mock.create_autospec(stompman.Client, instance=True)
    client_mock.servers = [connection_parameters]
    return typing.cast("stompman.Client", client_mock), client_mock


class TestTesting:
    async def test_integration(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
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

    async def test_non_string_header_values_raise_type_error(
        self, faker: faker.Faker, broker: faststream_stomp.StompBroker
    ) -> None:
        # gotcha: dict[str, typing.Any] is easily happily into dict[str, str]
        headers: dict[str, typing.Any] = {"key": 123}

        @broker.subscriber(destination := faker.pystr())
        def handle(body: str) -> None: ...

        async with faststream_stomp.TestStompBroker(broker) as br:
            with pytest.raises(TypeError):
                await br.publish(faker.pystr(), destination, headers=headers)

    async def test_publish_pydantic(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        class SomePydanticModel(pydantic.BaseModel):
            foo: str

        async with faststream_stomp.TestStompBroker(broker) as br:
            await br.publish(ModelFactory.create_factory(SomePydanticModel).build(), faker.pystr())

    async def test_routers(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        router = StompRouter()
        broker.include_router(router)

        @router.subscriber(destination := faker.pystr())
        def handle_message(body: str) -> None: ...

        async with faststream_stomp.TestStompBroker(broker):
            await broker.publish(faker.pystr(), destination)
            assert handle_message.mock
            handle_message.mock.assert_called_once()

    async def test_batch(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        messages_count = faker.pyint(min_value=3, max_value=20)

        @broker.subscriber(destination := faker.pystr())
        def handle_message(body: str) -> None: ...

        async with faststream_stomp.TestStompBroker(broker):
            await broker.publish_batch(*(faker.pystr() for _ in range(messages_count)), destination=destination)

            assert handle_message.mock
            assert handle_message.mock.call_count == messages_count


class TestNotImplemented:
    async def test_broker_request(self, faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
        async with faststream_stomp.TestStompBroker(broker):
            with pytest.raises(NotImplementedError):
                await broker.request(faker.pystr(), faker.pystr())

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


class TestAddContentLength:
    @pytest.mark.parametrize(
        ("broker_default", "call_override", "expected"),
        [
            (True, None, True),
            (False, None, False),
            (False, True, True),
            (True, False, False),
        ],
    )
    async def test_broker_publish(
        self,
        fake_connection_params: stompman.ConnectionParameters,
        faker: faker.Faker,
        *,
        broker_default: bool,
        call_override: bool | None,
        expected: bool,
    ) -> None:
        client, client_mock = make_mock_client(fake_connection_params)
        broker = faststream_stomp.StompBroker(client, add_content_length=broker_default)

        await broker.publish(faker.pystr(), faker.pystr(), add_content_length=call_override)

        assert client_mock.send.await_args.kwargs["add_content_length"] is expected

    @pytest.mark.parametrize(
        ("broker_default", "publisher_default", "call_override", "expected"),
        [
            (True, None, None, True),
            (False, None, None, False),
            (True, False, None, False),
            (False, True, None, True),
            (True, True, False, False),
            (False, False, True, True),
        ],
    )
    async def test_publisher_publish(
        self,
        fake_connection_params: stompman.ConnectionParameters,
        faker: faker.Faker,
        *,
        broker_default: bool,
        publisher_default: bool | None,
        call_override: bool | None,
        expected: bool,
    ) -> None:
        client, client_mock = make_mock_client(fake_connection_params)
        broker = faststream_stomp.StompBroker(client, add_content_length=broker_default)
        publisher = broker.publisher(faker.pystr(), add_content_length=publisher_default)

        await publisher.publish(faker.pystr(), add_content_length=call_override)

        assert client_mock.send.await_args.kwargs["add_content_length"] is expected

    async def test_publish_batch(
        self, fake_connection_params: stompman.ConnectionParameters, faker: faker.Faker
    ) -> None:
        client, client_mock = make_mock_client(fake_connection_params)
        broker = faststream_stomp.StompBroker(client)
        transaction = mock.AsyncMock()
        messages = (faker.pystr(), faker.pystr())

        client_mock.begin.return_value.__aenter__.return_value = transaction
        await broker.publish_batch(
            *messages,
            destination=faker.pystr(),
            add_content_length=False,
        )

        assert transaction.send.await_count == len(messages)
        assert all(call.kwargs["add_content_length"] is False for call in transaction.send.await_args_list)

    async def test_publisher_decorator_default(self, broker: faststream_stomp.StompBroker, faker: faker.Faker) -> None:
        publisher = broker.publisher(faker.pystr(), add_content_length=False)
        publish_mock = mock.AsyncMock()
        command = PublishCommand(faker.pystr(), _publish_type=PublishType.PUBLISH)

        with mock.patch.object(broker.config.producer, "publish", publish_mock):
            await publisher._publish(command, _extra_middlewares=())

        assert publish_mock.await_args is not None
        published_command = publish_mock.await_args.args[0]
        assert published_command.add_content_length is False

    def test_subscriber_reply_default(self, broker: faststream_stomp.StompBroker, faker: faker.Faker) -> None:
        subscriber = broker.subscriber(faker.pystr(), reply_add_content_length=False)
        response_publisher = typing.cast(
            "StompFakePublisher",
            subscriber._make_response_publisher(mock.Mock(reply_to=faker.pystr()))[0],
        )

        command = response_publisher.patch_command(PublishCommand(faker.pystr(), _publish_type=PublishType.REPLY))

        assert command.add_content_length is False


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


def test_handle_listen_task_done_logs_unhandled_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # faststream parent logger has propagate=False, which would block caplog (attached to root)
    monkeypatch.setattr(logging.getLogger("faststream"), "propagate", True)

    async def run() -> None:
        async def boom() -> None:  # noqa: RUF029
            msg = "kaboom"
            raise RuntimeError(msg)

        task: asyncio.Task[None] = asyncio.create_task(boom())
        with contextlib.suppress(RuntimeError):
            await task

        with caplog.at_level(logging.ERROR):
            _handle_listen_task_done(task)

    asyncio.run(run())

    assert any("listen task exited" in m.lower() for m in caplog.messages)
