import asyncio
from typing import Any

import pytest
from stompman import (
    SendFrame,
)
from stompman.frames import SendHeaders

from test_stompman.conftest import (
    EnrichedClient,
    create_spying_connection,
    enrich_expected_frames,
    get_read_frames_with_lifespan,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("args", "expected_body", "expected_headers"),
    [
        (
            {"body": b"Some body", "destination": "Some/queue"},
            b"Some body",
            {"content-length": "9", "destination": "Some/queue"},
        ),
        (
            {"body": b"Some body", "destination": "Some/queue", "add_content_length": True},
            b"Some body",
            {"content-length": "9", "destination": "Some/queue"},
        ),
        (
            {"body": b"Some body", "destination": "Some/queue", "content_type": "text/plain"},
            b"Some body",
            {"content-length": "9", "destination": "Some/queue", "content-type": "text/plain"},
        ),
        (
            {"body": b"Some body", "destination": "Some/queue", "add_content_length": False},
            b"Some body",
            {"destination": "Some/queue"},
        ),
    ],
)
async def test_send_message(args: dict[str, Any], expected_body: bytes, expected_headers: SendHeaders) -> None:
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([]))

    async with EnrichedClient(connection_class=connection_class) as client:
        await client.send(**args)
        await asyncio.sleep(0)

    assert collected_frames == enrich_expected_frames(
        SendFrame(headers=expected_headers, body=expected_body),
    )
