import time

import pytest
from stompman import Client, ConnectedFrame, ReceiptFrame

from test_stompman.conftest import EnrichedClient, build_dataclass, create_spying_connection

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("mock_sleep")]


@pytest.mark.parametrize("is_alive", [True, False])
async def test_connection_alive(is_alive: bool) -> None:  # noqa: FBT001
    connection_class, _ = create_spying_connection(
        [ConnectedFrame(headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "1000,1000"})],
        [],
        [build_dataclass(ReceiptFrame)],
    )
    client = await EnrichedClient(connection_class=connection_class).__aenter__()
    assert client._connection_manager._active_connection_state
    client._connection_manager._active_connection_state.connection.last_read_time = time.time() - 1 + is_alive
    assert client.is_alive() == is_alive
