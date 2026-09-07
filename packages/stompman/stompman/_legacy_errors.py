"""Translate native diagnostics without changing the public exception families."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

from .config import ConnectionParameters
from .core import errors as native
from .errors import AllServersUnavailable, AnyConnectionIssue, FailedAllConnectAttemptsError


def _flush_unavailable(
    reports: list[native.AllServersUnavailable],
    issues: list[AnyConnectionIssue],
    servers: list[ConnectionParameters],
    timeout: int | None,
) -> None:
    if reports:
        original_timeout = cast("int", reports[0].timeout) if timeout is None else timeout
        issues.append(AllServersUnavailable(servers=servers, timeout=original_timeout))
        reports.clear()


def _translate_issues(
    error: native.FailedAllConnectAttemptsError, servers: list[ConnectionParameters], timeout: int | None
) -> list[AnyConnectionIssue]:
    issues: list[AnyConnectionIssue] = []
    unavailable: list[native.AllServersUnavailable] = []
    for issue in error.issues:
        if isinstance(issue, native.AllServersUnavailable) and not isinstance(issue, AllServersUnavailable):
            unavailable.append(issue)
            if len(unavailable) == len(servers):
                _flush_unavailable(unavailable, issues, servers, timeout)
        else:
            _flush_unavailable(unavailable, issues, servers, timeout)
            # LegacyProtocol only produces the original handshake outcomes.
            issues.append(cast("AnyConnectionIssue", issue))
    _flush_unavailable(unavailable, issues, servers, timeout)
    return issues


def _connection_failure(
    error: native.FailedAllConnectAttemptsError, servers: list[ConnectionParameters], timeout: int | None
) -> native.FailedAllConnectAttemptsError:
    issues = _translate_issues(error, servers, timeout)
    if (
        isinstance(error, FailedAllConnectAttemptsError)
        and len(issues) == len(error.issues)
        and all(new is old for new, old in zip(issues, error.issues, strict=True))
    ):
        return error
    translated = FailedAllConnectAttemptsError(retry_attempts=error.retry_attempts, issues=issues)
    for note in getattr(error, "__notes__", ()):
        translated.add_note(note)
    return translated


def _translate(error: BaseException, servers: list[ConnectionParameters], timeout: int | None) -> BaseException:
    if isinstance(error, native.FailedAllConnectAttemptsError):
        return _connection_failure(error, servers, timeout)
    if not isinstance(error, BaseExceptionGroup):
        return error
    children = tuple(_translate(child, servers, timeout) for child in error.exceptions)
    if all(new is old for new, old in zip(children, error.exceptions, strict=True)):
        return error
    translated = error.derive(children)
    translated.__traceback__ = error.__traceback__
    translated.__cause__ = error.__cause__
    translated.__context__ = error.__context__
    translated.__suppress_context__ = error.__suppress_context__
    for note in getattr(error, "__notes__", ()):
        translated.add_note(note)
    return translated


@contextmanager
def translate_connection_errors(servers: list[ConnectionParameters], *, timeout: int | None = None) -> Iterator[None]:
    try:
        yield
    except (native.FailedAllConnectAttemptsError, BaseExceptionGroup) as error:
        translated = _translate(error, servers, timeout)
        if translated is error:
            raise
        raise translated from error
