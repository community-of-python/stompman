from typing import Any, assert_never

import pytest
import stompman
from stompman.errors import AllServersUnavailable, AnyConnectionIssue, ConnectionLostOnLifespanEnter

from test_stompman.conftest import build_dataclass


@pytest.mark.parametrize(
    "class_",
    [stompman.ConnectionLostError, stompman.FailedAllConnectAttemptsError, stompman.FailedAllWriteAttemptsError],
)
def test_error_str(class_: Any) -> None:  # ruff: ignore[any-type]
    error = build_dataclass(class_)
    assert str(error) == repr(error)


def describe_protocol_issue(issue: stompman.StompProtocolConnectionIssue) -> str:
    if isinstance(issue, stompman.ConnectionConfirmationTimeout):
        return str(issue.timeout)
    return issue.given_version


def describe_connection_issue(issue: AnyConnectionIssue) -> str:
    if isinstance(issue, stompman.ConnectionConfirmationTimeout | stompman.UnsupportedProtocolVersion):
        return describe_protocol_issue(issue)
    if isinstance(issue, AllServersUnavailable):
        return str(issue.timeout)
    if isinstance(issue, ConnectionLostOnLifespanEnter):
        return "connection lost"
    assert_never(issue)


@pytest.mark.parametrize(
    ("issue", "expected"),
    [
        (stompman.ConnectionConfirmationTimeout(timeout=1, frames=[]), "1"),
        (stompman.UnsupportedProtocolVersion(given_version="1.1", supported_version="1.2"), "1.1"),
        (AllServersUnavailable(servers=[], timeout=2), "2"),
        (ConnectionLostOnLifespanEnter(), "connection lost"),
    ],
)
def test_legacy_connection_issues_remain_exhaustively_typed(issue: AnyConnectionIssue, expected: str) -> None:
    error = stompman.FailedAllConnectAttemptsError(retry_attempts=1, issues=[issue])
    assert describe_connection_issue(error.issues[0]) == expected
