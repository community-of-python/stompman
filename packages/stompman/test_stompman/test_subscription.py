import asyncio
import logging
from collections.abc import AsyncGenerator
from functools import partial
from typing import Final, Self, get_args
from unittest import mock

import faker
import pytest
import stompman.subscription
from stompman import (
    AckFrame,
    AckMode,
    ConnectedFrame,
    ConnectionLostError,
    ErrorFrame,
    FailedAllConnectAttemptsError,
    HeartbeatFrame,
    MessageFrame,
    NackFrame,
    ReceiptFrame,
    SendFrame,
    SubscribeFrame,
    UnsubscribeFrame,
)

from test_stompman.conftest import (
    CONNECT_FRAME,
    CONNECTED_FRAME,
    BaseMockConnection,
    EnrichedClient,
    SomeError,
    build_dataclass,
    create_spying_connection,
    enrich_expected_frames,
    get_read_frames_with_lifespan,
    noop_error_handler,
    noop_message_handler,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("ack", get_args(AckMode))
async def test_client_subscriptions_lifespan_resubscribe(ack: AckMode, faker: faker.Faker) -> None:
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([CONNECTED_FRAME], []))
    client = EnrichedClient(connection_class=connection_class)
    sub_destination, message_destination, message_body = faker.pystr(), faker.pystr(), faker.binary(length=10)
    sub_extra_headers = faker.pydict(value_types=[str])

    async with client:
        subscription = await client.subscribe(
            destination=sub_destination,
            handler=noop_message_handler,
            ack=ack,
            headers=sub_extra_headers,
            on_suppressed_exception=noop_error_handler,
        )
        client._connection_manager._clear_active_connection_state(build_dataclass(ConnectionLostError))
        await client.send(message_body, destination=message_destination)
        await subscription.unsubscribe()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    subscribe_frame = SubscribeFrame(
        headers={
            "id": subscription.id,
            "destination": sub_destination,
            "ack": ack,
            **sub_extra_headers,  # type: ignore[typeddict-item]
        }
    )
    assert collected_frames == enrich_expected_frames(
        subscribe_frame,
        CONNECT_FRAME,
        CONNECTED_FRAME,
        subscribe_frame,
        SendFrame(
            headers={"destination": message_destination, "content-length": str(len(message_body))}, body=message_body
        ),
        UnsubscribeFrame(headers={"id": subscription.id}),
    )


async def test_client_subscriptions_lifespan_no_active_subs_in_aexit(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker
) -> None:
    monkeypatch.setattr(
        stompman.subscription,
        "_make_subscription_id",
        mock.Mock(side_effect=[(first_id := faker.pystr()), (second_id := faker.pystr())]),
    )
    first_destination, second_destination = faker.pystr(), faker.pystr()
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([]))

    async with EnrichedClient(connection_class=connection_class) as client:
        first_subscription = await client.subscribe(
            first_destination, handler=noop_message_handler, on_suppressed_exception=noop_error_handler
        )
        second_subscription = await client.subscribe(
            second_destination, handler=noop_message_handler, on_suppressed_exception=noop_error_handler
        )
        await asyncio.sleep(0)
        await first_subscription.unsubscribe()
        await second_subscription.unsubscribe()

    assert collected_frames == enrich_expected_frames(
        SubscribeFrame(headers={"id": first_id, "destination": first_destination, "ack": "client-individual"}),
        SubscribeFrame(headers={"id": second_id, "destination": second_destination, "ack": "client-individual"}),
        UnsubscribeFrame(headers={"id": first_id}),
        UnsubscribeFrame(headers={"id": second_id}),
    )


@pytest.mark.parametrize("direct_error", [True, False])
async def test_client_subscriptions_lifespan_with_active_subs_in_aexit(
    monkeypatch: pytest.MonkeyPatch,
    faker: faker.Faker,
    *,
    direct_error: bool,
) -> None:
    subscription_id, destination = faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([]))

    if direct_error:
        with pytest.raises(SomeError):  # noqa: PT012
            async with EnrichedClient(connection_class=connection_class) as client:
                await client.subscribe(
                    destination, handler=noop_message_handler, on_suppressed_exception=noop_error_handler
                )
                await SomeError.raise_after_tick()
    else:
        with pytest.raises(ExceptionGroup) as exc_info:  # noqa: PT012
            async with asyncio.TaskGroup() as task_group, EnrichedClient(connection_class=connection_class) as client:
                await client.subscribe(
                    destination, handler=noop_message_handler, on_suppressed_exception=noop_error_handler
                )
                task_group.create_task(SomeError.raise_after_tick())

        assert exc_info.value.exceptions == (SomeError(),)

    assert collected_frames == enrich_expected_frames(
        SubscribeFrame(headers={"id": subscription_id, "destination": destination, "ack": "client-individual"}),
        UnsubscribeFrame(headers={"id": subscription_id}),
    )


async def test_client_listen_routing_ok(monkeypatch: pytest.MonkeyPatch, faker: faker.Faker) -> None:
    monkeypatch.setattr(
        stompman.subscription,
        "_make_subscription_id",
        mock.Mock(side_effect=[(first_sub_id := faker.pystr()), (second_sub_id := faker.pystr())]),
    )
    connection_class, _ = create_spying_connection(
        *get_read_frames_with_lifespan(
            [
                build_dataclass(ConnectedFrame),
                build_dataclass(ReceiptFrame),
                (first_message_frame := build_dataclass(MessageFrame, headers={"subscription": first_sub_id})),
                (error_frame := build_dataclass(ErrorFrame)),
                (_second_message_frame := build_dataclass(MessageFrame)),
                (third_message_frame := build_dataclass(MessageFrame, headers={"subscription": second_sub_id})),
                HeartbeatFrame(),
            ]
        )
    )
    first_message_handler, first_error_handler = mock.AsyncMock(return_value=None), mock.Mock()
    second_message_handler, second_error_handler = mock.AsyncMock(side_effect=SomeError), mock.Mock()

    async with EnrichedClient(
        connection_class=connection_class, on_error_frame=(on_error_frame := mock.Mock())
    ) as client:
        first_subscription = await client.subscribe(
            faker.pystr(), handler=first_message_handler, on_suppressed_exception=first_error_handler
        )
        second_subscription = await client.subscribe(
            faker.pystr(), handler=second_message_handler, on_suppressed_exception=second_error_handler
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await first_subscription.unsubscribe()
        await second_subscription.unsubscribe()

    first_message_handler.assert_called_once_with(first_message_frame)
    first_error_handler.assert_not_called()

    second_message_handler.assert_called_once_with(third_message_frame)
    second_error_handler.assert_called_once_with(SomeError(), third_message_frame)

    on_error_frame.assert_called_once_with(error_frame)


@pytest.mark.parametrize("side_effect", [None, SomeError])
@pytest.mark.parametrize("ack", ["client", "client-individual"])
async def test_client_listen_unsubscribe_before_ack_or_nack(
    monkeypatch: pytest.MonkeyPatch,
    faker: faker.Faker,
    ack: AckMode,
    side_effect: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscription_id, destination = faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))

    message_frame = build_dataclass(MessageFrame, headers={"subscription": subscription_id})
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([message_frame]))
    message_handler = mock.AsyncMock(side_effect=side_effect)

    async with EnrichedClient(connection_class=connection_class) as client:
        subscription = await client.subscribe(
            destination, message_handler, on_suppressed_exception=noop_error_handler, ack=ack
        )
        await asyncio.sleep(0)
        await subscription.unsubscribe()
        await asyncio.sleep(0)

    message_handler.assert_called_once_with(message_frame)
    assert collected_frames == enrich_expected_frames(
        SubscribeFrame(headers={"ack": ack, "destination": destination, "id": subscription_id}),
        message_frame,
        UnsubscribeFrame(headers={"id": subscription_id}),
    )
    assert len(caplog.messages) == 1


@pytest.mark.parametrize("side_effect", [None, SomeError])
@pytest.mark.parametrize("ack", ["client", "client-individual"])
async def test_client_listen_ack_with_no_ack_header(
    monkeypatch: pytest.MonkeyPatch,
    faker: faker.Faker,
    ack: AckMode,
    side_effect: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscription_id, destination = faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))

    message_frame = build_dataclass(MessageFrame, headers={"subscription": subscription_id})
    message_frame.headers.pop("ack", None)

    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([message_frame]))
    message_handler = mock.AsyncMock(side_effect=side_effect)

    async with EnrichedClient(connection_class=connection_class) as client:
        subscription = await client.subscribe(
            destination, message_handler, on_suppressed_exception=noop_error_handler, ack=ack
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await subscription.unsubscribe()

    message_handler.assert_called_once_with(message_frame)
    assert collected_frames == enrich_expected_frames(
        SubscribeFrame(headers={"ack": ack, "destination": destination, "id": subscription_id}),
        message_frame,
        UnsubscribeFrame(headers={"id": subscription_id}),
    )
    assert len(caplog.messages) == 1


@pytest.mark.parametrize("ok", [True, False])
@pytest.mark.parametrize("ack", ["client", "client-individual"])
async def test_client_listen_ack_nack_sent(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker, ack: AckMode, *, ok: bool
) -> None:
    subscription_id, destination, ack_id = faker.pystr(), faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))

    message_frame = build_dataclass(
        MessageFrame, headers={"destination": destination, "ack": ack_id, "subscription": subscription_id}
    )
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([message_frame]))
    message_handler = mock.AsyncMock(side_effect=None if ok else SomeError)

    async with EnrichedClient(connection_class=connection_class) as client:
        subscription = await client.subscribe(
            destination, message_handler, on_suppressed_exception=noop_error_handler, ack=ack
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await subscription.unsubscribe()

    message_handler.assert_called_once_with(message_frame)
    assert collected_frames == enrich_expected_frames(
        SubscribeFrame(headers={"id": subscription_id, "destination": destination, "ack": ack}),
        message_frame,
        AckFrame(headers={"id": ack_id, "subscription": subscription_id})
        if ok
        else NackFrame(headers={"id": ack_id, "subscription": subscription_id}),
        UnsubscribeFrame(headers={"id": subscription_id}),
    )


@pytest.mark.parametrize("ok", [True, False])
async def test_client_listen_auto_ack_nack(monkeypatch: pytest.MonkeyPatch, faker: faker.Faker, *, ok: bool) -> None:
    subscription_id, destination, message_id = faker.pystr(), faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))

    message_frame = build_dataclass(
        MessageFrame, headers={"destination": destination, "message-id": message_id, "subscription": subscription_id}
    )
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([message_frame]))
    message_handler = mock.AsyncMock(side_effect=None if ok else SomeError)

    async with EnrichedClient(connection_class=connection_class) as client:
        subscription = await client.subscribe(
            destination, message_handler, on_suppressed_exception=noop_error_handler, ack="auto"
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await subscription.unsubscribe()

    message_handler.assert_called_once_with(message_frame)
    assert collected_frames == enrich_expected_frames(
        SubscribeFrame(headers={"ack": "auto", "destination": destination, "id": subscription_id}),
        message_frame,
        UnsubscribeFrame(headers={"id": subscription_id}),
    )


async def test_client_listen_manual_ack_nack_ok(monkeypatch: pytest.MonkeyPatch, faker: faker.Faker) -> None:
    subscription_id, destination, message_id, ack_id = faker.pystr(), faker.pystr(), faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))

    message_frame = build_dataclass(
        MessageFrame,
        headers={"destination": destination, "message-id": message_id, "subscription": subscription_id, "ack": ack_id},
    )
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([message_frame]))

    async def handle_message(message: stompman.subscription.AckableMessageFrame) -> None:
        await message.ack()
        await message.nack()

    async with EnrichedClient(connection_class=connection_class) as client:
        subscription = await client.subscribe_with_manual_ack(destination, handle_message)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await subscription.unsubscribe()

    assert collected_frames == enrich_expected_frames(
        SubscribeFrame(headers={"ack": "client-individual", "destination": destination, "id": subscription_id}),
        message_frame,
        AckFrame(headers={"subscription": subscription_id, "id": ack_id}),
        NackFrame(headers={"subscription": subscription_id, "id": ack_id}),
        UnsubscribeFrame(headers={"id": subscription_id}),
    )


async def test_client_listen_raises_on_aexit(monkeypatch: pytest.MonkeyPatch, faker: faker.Faker) -> None:
    monkeypatch.setattr("asyncio.sleep", partial(asyncio.sleep, 0))

    connection_class, _ = create_spying_connection(*get_read_frames_with_lifespan([]))
    connection_class.connect = mock.AsyncMock(side_effect=[connection_class(), None, None, None])  # type: ignore[method-assign]

    async def close_connection_soon(client: stompman.Client) -> None:
        await asyncio.sleep(0)
        client._connection_manager._clear_active_connection_state(build_dataclass(ConnectionLostError))

    with pytest.raises(ExceptionGroup) as exc_info:  # noqa: PT012
        async with asyncio.TaskGroup() as task_group, EnrichedClient(connection_class=connection_class) as client:
            await client.subscribe(faker.pystr(), noop_message_handler, on_suppressed_exception=noop_error_handler)
            task_group.create_task(close_connection_soon(client))

    assert len(exc_info.value.exceptions) == 1
    inner_group = exc_info.value.exceptions[0]

    assert isinstance(inner_group, ExceptionGroup)
    assert len(inner_group.exceptions) == 1

    inner_inner_group = inner_group.exceptions[0]
    assert isinstance(inner_inner_group, ExceptionGroup)
    assert len(inner_inner_group.exceptions) == 1

    assert isinstance(inner_inner_group.exceptions[0], FailedAllConnectAttemptsError)


async def test_subscription_sends_ack_for_message_received_after_reconnection(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker
) -> None:
    subscription_id, destination, message_id, ack_id = faker.pystr(), faker.pystr(), faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))
    message_frame = build_dataclass(
        MessageFrame,
        headers={"destination": destination, "message-id": message_id, "subscription": subscription_id, "ack": ack_id},
    )
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([CONNECTED_FRAME], []))

    async with EnrichedClient(connection_class=connection_class) as client:
        subscription = await client.subscribe_with_manual_ack(destination, noop_message_handler)
        client._connection_manager._clear_active_connection_state(build_dataclass(ConnectionLostError))
        await client.send(b"trigger-reconnect", destination=destination)
        message_after_reconnect = stompman.subscription.AckableMessageFrame(
            headers=message_frame.headers,
            body=message_frame.body,
            _subscription=subscription,
            _received_at_reconnection_count=client._connection_manager._reconnection_count,
        )
        await message_after_reconnect.ack()
        await message_after_reconnect.nack()
        await subscription.unsubscribe()

    assert client._connection_manager._reconnection_count == 1, "reconnect should have happened"
    ack_frames = [one_frame for one_frame in collected_frames if isinstance(one_frame, AckFrame)]
    nack_frames = [one_frame for one_frame in collected_frames if isinstance(one_frame, NackFrame)]
    assert ack_frames == [AckFrame(headers={"subscription": subscription_id, "id": ack_id})], (
        "ack must be sent for messages received on the new connection after reconnect"
    )
    assert nack_frames == [NackFrame(headers={"subscription": subscription_id, "id": ack_id})], (
        "nack must be sent for messages received on the new connection after reconnect"
    )


async def test_subscription_skips_ack_nack_after_reconnection(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker, caplog: pytest.LogCaptureFixture
) -> None:
    subscription_id, destination, message_id, ack_id = faker.pystr(), faker.pystr(), faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))
    message_frame = build_dataclass(
        MessageFrame,
        headers={"destination": destination, "message-id": message_id, "subscription": subscription_id, "ack": ack_id},
    )
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([message_frame]))
    stored_message = None

    async def track_ack_nack_frames(message: stompman.subscription.AckableMessageFrame) -> None:
        nonlocal stored_message
        stored_message = message
        await asyncio.sleep(0)

    async with EnrichedClient(connection_class=connection_class) as client:
        subscription = await client.subscribe_with_manual_ack(destination, track_ack_nack_frames)
        await asyncio.sleep(0)
        client._connection_manager._clear_active_connection_state(build_dataclass(ConnectionLostError))
        await asyncio.sleep(0)

        with caplog.at_level(logging.DEBUG, logger="stompman"):
            assert stored_message
            await stored_message.ack()
            await stored_message.nack()

        await subscription.unsubscribe()

    assert not [one_frame for one_frame in collected_frames if isinstance(one_frame, AckFrame)]
    assert not [one_frame for one_frame in collected_frames if isinstance(one_frame, NackFrame)]
    assert any(
        "connection changed since message was received" in one_message.lower() for one_message in caplog.messages
    )


async def test_subscription_skips_ack_for_message_consumed_after_concurrent_clear(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker, caplog: pytest.LogCaptureFixture
) -> None:
    subscription_id, destination, message_id, ack_id = faker.pystr(), faker.pystr(), faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))
    message_frame = build_dataclass(
        MessageFrame,
        headers={"destination": destination, "message-id": message_id, "subscription": subscription_id, "ack": ack_id},
    )

    gate = asyncio.Event()
    received_messages: list[stompman.subscription.AckableMessageFrame] = []
    collected_frames: list[stompman.AnyClientFrame | stompman.AnyServerFrame] = []
    next_connection_id = 0
    listener_read_call_index: Final = 2

    async def store_message_handler(message: stompman.subscription.AckableMessageFrame) -> None:  # noqa: RUF029
        received_messages.append(message)

    class GatedConnection(BaseMockConnection):
        connection_id: int
        read_call: int

        @classmethod
        async def connect(cls, **_kwargs: object) -> Self:
            nonlocal next_connection_id
            next_connection_id += 1
            instance = cls()
            instance.connection_id = next_connection_id
            instance.read_call = 0
            return instance

        async def write_frame(self, frame: stompman.AnyClientFrame) -> None:
            collected_frames.append(frame)

        async def read_frames(self) -> AsyncGenerator[stompman.AnyServerFrame, None]:  # type: ignore[override]
            self.read_call += 1
            # gate the listener's read on the first connection so a concurrent task can clear state
            # while the listener is mid-iteration on its read_frames generator
            if self.connection_id == 1 and self.read_call == listener_read_call_index:
                await gate.wait()
                collected_frames.append(message_frame)
                yield message_frame
                await asyncio.Future()
                return
            collected_frames.append(CONNECTED_FRAME)
            yield CONNECTED_FRAME
            await asyncio.Future()

    async with EnrichedClient(connection_class=GatedConnection) as client:
        subscription = await client.subscribe_with_manual_ack(destination, store_message_handler)
        await asyncio.sleep(0)
        client._connection_manager._clear_active_connection_state(build_dataclass(ConnectionLostError))
        assert client._connection_manager._reconnection_count == 1
        await client.send(b"trigger-reconnect", destination=destination)
        assert client._connection_manager._active_connection_state is not None
        gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if received_messages:
                break
        assert received_messages

        with caplog.at_level(logging.DEBUG, logger="stompman"):
            await received_messages[0].ack()

        await subscription.unsubscribe()

    assert not [one_frame for one_frame in collected_frames if isinstance(one_frame, AckFrame)]
    assert any(
        "connection changed since message was received" in one_message.lower() for one_message in caplog.messages
    )


async def test_auto_ack_handler_unhandled_exception_does_not_kill_listener(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker, caplog: pytest.LogCaptureFixture
) -> None:
    subscription_id, destination, ack_id_a, ack_id_b = faker.pystr(), faker.pystr(), faker.pystr(), faker.pystr()
    monkeypatch.setattr(stompman.subscription, "_make_subscription_id", mock.Mock(return_value=subscription_id))

    message_a = build_dataclass(
        MessageFrame,
        headers={"destination": destination, "message-id": "a", "subscription": subscription_id, "ack": ack_id_a},
    )
    message_b = build_dataclass(
        MessageFrame,
        headers={"destination": destination, "message-id": "b", "subscription": subscription_id, "ack": ack_id_b},
    )

    class Boom(BaseException):  # not Exception: NOT in suppressed_exception_classes
        pass

    handled: list[str] = []

    async def handler(frame: MessageFrame) -> None:
        handled.append(frame.headers["message-id"])
        if frame.headers["message-id"] == "a":
            raise Boom

    connection_class, _collected_frames = create_spying_connection(
        *get_read_frames_with_lifespan([message_a, message_b])
    )

    async with EnrichedClient(connection_class=connection_class) as client:
        with caplog.at_level(logging.ERROR, logger="stompman"):
            subscription = await client.subscribe(
                destination,
                handler,
                on_suppressed_exception=noop_error_handler,
                suppressed_exception_classes=(),  # nothing suppressed
            )
            for _ in range(20):
                await asyncio.sleep(0)
                if len(handled) == 2:
                    break
            await subscription.unsubscribe()

    assert handled == ["a", "b"], "second message must be handled after first one crashed"
    assert any("unhandled exception in message handler" in m.lower() for m in caplog.messages)


def test_make_subscription_id() -> None:
    stompman.subscription._make_subscription_id()


async def wait_and_unsubscribe(*subscriptions: stompman.subscription.BaseSubscription, wait_in_seconds: float) -> None:
    await asyncio.sleep(wait_in_seconds)
    for subscription in subscriptions:
        await subscription.unsubscribe()


async def test_client_exits_when_subscriptions_are_unsubscribed(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker
) -> None:
    monkeypatch.setattr(
        stompman.subscription,
        "_make_subscription_id",
        mock.Mock(side_effect=[(first_id := faker.pystr()), (second_id := faker.pystr())]),
    )
    first_destination, second_destination = faker.pystr(), faker.pystr()
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([]))

    async with EnrichedClient(connection_class=connection_class) as client:
        first_subscription = await client.subscribe(
            first_destination, handler=noop_message_handler, on_suppressed_exception=noop_error_handler
        )
        second_subscription = await client.subscribe(
            second_destination, handler=noop_message_handler, on_suppressed_exception=noop_error_handler
        )
        await asyncio.sleep(0)
        unsubscribe_task = asyncio.create_task(
            wait_and_unsubscribe(first_subscription, second_subscription, wait_in_seconds=0.5)
        )

    assert unsubscribe_task.done(), "Client should exit context manager only when subscriptions are unsubscribed"

    assert collected_frames == enrich_expected_frames(
        SubscribeFrame(headers={"id": first_id, "destination": first_destination, "ack": "client-individual"}),
        SubscribeFrame(headers={"id": second_id, "destination": second_destination, "ack": "client-individual"}),
        UnsubscribeFrame(headers={"id": first_id}),
        UnsubscribeFrame(headers={"id": second_id}),
    )
