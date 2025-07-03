import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from itertools import starmap
from typing import Final
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

DESTINATION: Final = "DLQ"


@asynccontextmanager
async def create_client(connection_parameters: stompman.ConnectionParameters) -> AsyncGenerator[stompman.Client, None]:
    async with stompman.Client(servers=[connection_parameters], connection_confirmation_timeout=10) as client:
        yield client


@pytest.mark.anyio
async def test_ok(connection_parameters: stompman.ConnectionParameters) -> None:
    async def produce() -> None:
        for message in messages[200:]:
            await producer.send(body=message, destination=DESTINATION, headers={"hello": "from outside transaction"})

        async with producer.begin() as transaction:
            for message in messages[:200]:
                await transaction.send(body=message, destination=DESTINATION, headers={"hello": "from transaction"})

    async def consume() -> None:
        received_messages = []
        event = asyncio.Event()

        async def handle_message(frame: stompman.MessageFrame) -> None:  # noqa: RUF029
            received_messages.append(frame.body)
            if len(received_messages) == len(messages):
                event.set()

        subscription = await consumer.subscribe(
            destination=DESTINATION, handler=handle_message, on_suppressed_exception=print
        )
        await asyncio.wait_for(event.wait(), timeout=5)
        await subscription.unsubscribe()

        assert sorted(received_messages) == sorted(messages)

    messages = [str(uuid4()).encode() for _ in range(1000)]

    async with (
        create_client(connection_parameters) as consumer,
        create_client(connection_parameters) as producer,
        asyncio.TaskGroup() as task_group,
    ):
        task_group.create_task(consume())
        task_group.create_task(produce())


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


noise_bytes_strategy = strategies.binary().filter(bytes_not_contains(NEWLINE, NULL))
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
