import platform
from typing import cast

import pytest
import stompman


@pytest.fixture(
    params=[
        pytest.param(("asyncio", {"use_uvloop": True}), id="asyncio+uvloop"),
        pytest.param(("asyncio", {"use_uvloop": False}), id="asyncio"),
    ],
)
def anyio_backend(request: pytest.FixtureRequest) -> object:
    return request.param


@pytest.fixture
def first_server_connection_parameters() -> stompman.ConnectionParameters:
    return stompman.ConnectionParameters(host="127.0.0.1", port=9000, login="admin", passcode=":=123")


@pytest.fixture(
    params=[
        stompman.ConnectionParameters(host="127.0.0.1", port=9000, login="admin", passcode=":=123"),
        stompman.ConnectionParameters(host="127.0.0.1", port=9001, login="admin", passcode=":=123"),
    ]
    if platform.platform() == "Linux"  # TODO: fix tests with ActiveMQ Classic on Mac  # noqa: FIX002, TD002, TD003
    else [
        stompman.ConnectionParameters(host="127.0.0.1", port=9000, login="admin", passcode=":=123"),
    ],
)
def connection_parameters(request: pytest.FixtureRequest) -> stompman.ConnectionParameters:
    return cast("stompman.ConnectionParameters", request.param)
