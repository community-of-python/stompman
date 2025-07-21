import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated
from unittest import mock

import faker
import faststream_stomp
import pytest
import stompman
from asgi_lifespan import LifespanManager
from faststream import BaseMiddleware, Context, FastStream
from faststream.asgi import AsgiFastStream
from faststream.exceptions import AckMessage, NackMessage, RejectMessage
from faststream.message import gen_cor_id
from faststream_stomp.models import StompStreamMessage
from faststream_stomp.router import StompRoutePublisher

if TYPE_CHECKING:
    from faststream_stomp.broker import StompBroker

pytestmark = pytest.mark.anyio


@pytest.fixture
def broker(first_server_connection_parameters: stompman.ConnectionParameters) -> faststream_stomp.StompBroker:
    return faststream_stomp.StompBroker(stompman.Client([first_server_connection_parameters]))


async def test_simple(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    app = FastStream(broker)
    expected_body, destination = faker.pystr(), faker.pystr()
    publisher = broker.publisher(destination)
    event = asyncio.Event()

    @broker.subscriber(destination)
    def _(body: str, message: stompman.MessageFrame = Context("message.raw_message")) -> None:  # noqa: B008
        assert body == expected_body
        event.set()

    @app.after_startup
    async def _() -> None:
        await broker.connect()
        await publisher.publish(expected_body.encode(), correlation_id=gen_cor_id())

    async with asyncio.timeout(10), asyncio.TaskGroup() as task_group:
        run_task = task_group.create_task(app.run())
        await event.wait()
        run_task.cancel()


async def test_republish(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    app = FastStream(broker)
    broker.add_middleware(BaseMiddleware)
    expected_body, first_destination, second_destination = faker.pystr(), faker.pystr(), faker.pystr()
    first_publisher, second_publisher = broker.publisher(first_destination), broker.publisher(second_destination)
    event = asyncio.Event()

    @broker.subscriber(first_destination)
    @second_publisher
    def _(body: str) -> str:
        return body

    @broker.subscriber(second_destination)
    def _(body: str) -> None:
        assert body == expected_body
        event.set()

    @app.after_startup
    async def _() -> None:
        await broker.connect()
        await first_publisher.publish(expected_body.encode())

    async with asyncio.timeout(10), asyncio.TaskGroup() as task_group:
        run_task = task_group.create_task(app.run())
        await event.wait()
        run_task.cancel()


async def test_router(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    expected_body, prefix, destination = faker.pystr(), faker.pystr(), faker.pystr()

    def route(body: str, message: stompman.MessageFrame = Context("message.raw_message")) -> bytes:  # noqa: B008
        assert body == expected_body
        event.set()
        return message.body

    router = faststream_stomp.StompRouter(
        prefix=prefix,
        handlers=[faststream_stomp.StompRoute(route, destination, publishers=[StompRoutePublisher(faker.pystr())])],
    )
    publisher = router.publisher(destination)

    broker.include_router(router)
    app = FastStream(broker)
    event = asyncio.Event()

    @app.after_startup
    async def _() -> None:
        await broker.connect()
        await publisher.publish(expected_body)

    async with asyncio.timeout(1), asyncio.TaskGroup() as task_group:
        run_task = task_group.create_task(app.run())
        await event.wait()
        run_task.cancel()


async def test_broker_close(broker: faststream_stomp.StompBroker) -> None:
    async with broker:
        pass


async def test_subscriber_lifespan(faker: faker.Faker, broker: faststream_stomp.StompBroker) -> None:
    @broker.subscriber(faker.pystr())
    def _() -> None: ...

    await broker.start()
    await broker.stop()


class TestPing:
    async def test_ok(self, broker: faststream_stomp.StompBroker) -> None:
        async with broker:
            assert await broker.ping()

    async def test_no_connection(self, broker: faststream_stomp.StompBroker) -> None:
        assert not await broker.ping()

    async def test_timeout(self, broker: faststream_stomp.StompBroker) -> None:
        async with broker:
            assert not await broker.ping(0)


@pytest.mark.parametrize("exception", [Exception, NackMessage, AckMessage, RejectMessage])
async def test_ack_nack_reject_exception(
    faker: faker.Faker, broker: faststream_stomp.StompBroker, exception: type[Exception]
) -> None:
    event = asyncio.Event()

    @broker.subscriber(destination := faker.pystr())
    def _() -> None:
        event.set()
        raise exception

    async with broker:
        await broker.start()
        await broker.publish(faker.pystr(), destination)
        await event.wait()


@pytest.mark.parametrize("method_name", ["ack", "nack", "reject"])
async def test_ack_nack_reject_method_call(
    faker: faker.Faker, broker: faststream_stomp.StompBroker, method_name: str
) -> None:
    event = asyncio.Event()

    @broker.subscriber(destination := faker.pystr())
    async def _(message: Annotated[StompStreamMessage, Context()]) -> None:
        await getattr(message, method_name)()
        event.set()

    async with broker:
        await broker.start()
        await broker.publish(faker.pystr(), destination)
        await event.wait()


class TestLogging:
    async def test_ok(
        self, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, faker: faker.Faker
    ) -> None:
        monkeypatch.delenv("PYTEST_CURRENT_TEST")
        broker: StompBroker = request.getfixturevalue("broker")
        broker.config.logger.logger = mock.Mock(log=(log_mock := mock.Mock()), handlers=[])

        @broker.subscriber(destination := faker.pystr())
        def some_handler() -> None: ...

        async with broker:
            await broker.start()

        assert log_mock.mock_calls == [
            mock.call(
                logging.INFO,
                "`SomeHandler` waiting for messages",
                extra={"destination": destination, "message_id": ""},
                exc_info=None,
            )
        ]

    async def test_raises(
        self, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, faker: faker.Faker
    ) -> None:
        monkeypatch.delenv("PYTEST_CURRENT_TEST")
        broker: StompBroker = request.getfixturevalue("broker")
        broker.config.broker_config.logger = mock.Mock(log=(log_mock := mock.Mock()))
        event = asyncio.Event()
        message_id: str | None = None

        @dataclass
        class MyError(Exception): ...

        @broker.subscriber(destination := faker.pystr())
        def some_handler(message_frame: Annotated[stompman.MessageFrame, Context("message.raw_message")]) -> None:
            nonlocal message_id
            message_id = message_frame.headers["message-id"]
            event.set()
            raise MyError

        async with broker:
            await broker.start()
            await broker.publish(faker.pystr(), destination)
            await event.wait()

        assert message_id
        extra = {"destination": destination, "message_id": message_id}
        assert log_mock.mock_calls[-3:] == [
            mock.call("Received", extra=extra),
            mock.call(message="MyError: ", extra=extra, exc_info=MyError()),
            mock.call(message="Processed", extra=extra),
        ]


async def test_broker_connect_twice(broker: faststream_stomp.StompBroker) -> None:
    app = AsgiFastStream(broker, on_startup=[broker.connect])
    async with LifespanManager(app):
        pass
