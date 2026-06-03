import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from itertools import starmap
from uuid import uuid4

import pytest
import stompman
from hypothesis import given, strategies
from stompman.serde import (
    COMMANDS_TO_FRAMES,
    NEWLINE,
    NULL,
    FrameParser,
    dump_frame,
    dump_header,
    make_frame_from_parts,
    parse_header,
)


async def wait_for_reconnect(client: stompman.Client, initial_reconnection_count: int) -> None:
    def is_reconnected() -> bool:
        return (
            client._connection_manager._reconnection_count > initial_reconnection_count
            and client._connection_manager._active_connection_state is not None
        )

    while not is_reconnected():  # noqa: ASYNC110
        await asyncio.sleep(0.05)


async def force_reconnect(client: stompman.Client) -> None:
    connection_state = await client._connection_manager._get_active_connection_state()
    initial_reconnection_count = client._connection_manager._reconnection_count
    client._connection_manager._clear_active_connection_state(stompman.ConnectionLostError(reason="test reconnect"))
    await connection_state.connection.close()
    await asyncio.wait_for(wait_for_reconnect(client, initial_reconnection_count), timeout=5)


@asynccontextmanager
async def create_client(connection_parameters: stompman.ConnectionParameters) -> AsyncGenerator[stompman.Client, None]:
    async with stompman.Client(servers=[connection_parameters], connection_confirmation_timeout=10) as client:
        yield client


@pytest.mark.anyio
async def test_consumption_survives_forced_reconnects(
    connection_parameters: stompman.ConnectionParameters,
) -> None:
    iterations = 3
    received: list[bytes] = []
    received_event = asyncio.Event()

    async def handle_message(frame: stompman.AckableMessageFrame) -> None:
        received.append(frame.body)
        await frame.ack()
        received_event.set()

    destination = "DLQ"

    async with (
        stompman.Client(servers=[connection_parameters], connection_confirmation_timeout=10) as consumer,
        stompman.Client(servers=[connection_parameters], connection_confirmation_timeout=10) as producer,
    ):

        async def consume_after_reconnects() -> None:
            for index in range(iterations):
                await force_reconnect(consumer)
                payload = f"msg-{index}-{uuid4()}".encode()
                received_event.clear()
                await producer.send(body=payload, destination=destination)
                await asyncio.wait_for(received_event.wait(), timeout=5)
                assert payload in received, f"iteration {index}: {payload!r} not delivered"

        subscription = await consumer.subscribe_with_manual_ack(destination=destination, handler=handle_message)
        try:
            await consume_after_reconnects()
        finally:
            await subscription.unsubscribe()

    assert len(received) == iterations, (
        f"expected exactly {iterations} deliveries, got {len(received)}: {received}. "
        "prior-acked messages are being redelivered after every forced reconnect"
    )


@pytest.mark.anyio
async def test_ok(connection_parameters: stompman.ConnectionParameters) -> None:
    async def produce(destination: str) -> None:
        await subscribed_event.wait()

        for message in messages[200:]:
            await producer.send(body=message, destination=destination, headers={"hello": "from outside transaction"})

        async with producer.begin() as transaction:
            for message in messages[:200]:
                await transaction.send(body=message, destination=destination, headers={"hello": "from transaction"})

    async def consume(destination: str) -> None:
        received_messages: list[bytes] = []
        event = asyncio.Event()

        async def handle_message(frame: stompman.MessageFrame) -> None:  # noqa: RUF029
            received_messages.append(frame.body)
            if len(received_messages) == len(messages):
                event.set()

        subscription = await consumer.subscribe(
            destination=destination, handler=handle_message, on_suppressed_exception=print
        )
        subscribed_event.set()
        await asyncio.wait_for(event.wait(), timeout=15)
        await subscription.unsubscribe()

        assert sorted(received_messages) == sorted(messages)

    messages = [str(uuid4()).encode() for _ in range(1000)]
    destination = "DLQ"
    subscribed_event = asyncio.Event()

    async with (
        create_client(connection_parameters) as consumer,
        create_client(connection_parameters) as producer,
        asyncio.TaskGroup() as task_group,
    ):
        task_group.create_task(consume(destination))
        task_group.create_task(produce(destination))


def generate_frames(
    cases: list[tuple[bytes, list[stompman.AnyClientFrame | stompman.AnyServerFrame]]],
) -> tuple[list[bytes], list[stompman.AnyClientFrame | stompman.AnyServerFrame]]:
    all_bytes, all_frames = [], []

    for noise, frames in cases:
        current_all_bytes = []
        if noise:
            current_all_bytes.append(noise + NEWLINE)

        for frame in frames:
            current_all_bytes.append(NEWLINE if isinstance(frame, stompman.HeartbeatFrame) else dump_frame(frame))
            all_frames.append(frame)

        all_bytes.append(b"".join(current_all_bytes))

    return all_bytes, all_frames


def bytes_not_contains(*avoided: bytes) -> Callable[[bytes], bool]:
    return lambda checked: all(item not in checked for item in avoided)


noise_bytes_strategy = strategies.binary().filter(bytes_not_contains(NEWLINE, NULL, *COMMANDS_TO_FRAMES))
header_value_strategy = strategies.text().filter(lambda text: "\x00" not in text)
headers_strategy = strategies.dictionaries(header_value_strategy, header_value_strategy).map(
    lambda headers: dict(
        parsed_header
        for header in starmap(dump_header, headers.items())
        if (parsed_header := parse_header(bytearray(header)))
    )
)

FRAMES_WITH_ESCAPED_HEADERS = tuple(command for command in COMMANDS_TO_FRAMES if command != b"CONNECT")
frame_strategy = strategies.just(stompman.HeartbeatFrame()) | strategies.builds(
    make_frame_from_parts,
    command=strategies.sampled_from(FRAMES_WITH_ESCAPED_HEADERS),
    headers=headers_strategy,
    body=strategies.binary().filter(bytes_not_contains(NULL)),
)


@given(
    strategies.builds(
        generate_frames,
        strategies.lists(strategies.tuples(noise_bytes_strategy, strategies.lists(frame_strategy))),
    ),
)
def test_parsing(case: tuple[list[bytes], list[stompman.AnyClientFrame | stompman.AnyServerFrame]]) -> None:
    stream_chunks, expected_frames = case
    parser = FrameParser()
    assert [frame for chunk in stream_chunks for frame in parser.parse_frames_from_chunk(chunk)] == expected_frames
