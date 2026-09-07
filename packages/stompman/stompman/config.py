from dataclasses import dataclass, field
from typing import Self, TypedDict, TypeVar
from urllib.parse import unquote

from stompman.core.config import Heartbeat

__all__ = ["ConnectionParameters", "Heartbeat", "MultiHostHostLike"]

_Value = TypeVar("_Value")


class MultiHostHostLike(TypedDict):
    username: str | None
    password: str | None
    host: str | None
    port: int | None


def _required(value: _Value | None, name: str) -> _Value:
    if value is None:
        msg = f"{name} must be set"
        raise ValueError(msg)
    return value


def _credentials(host: MultiHostHostLike) -> tuple[str, str] | None:
    username, password = host["username"], host["password"]
    if username is None:
        if password is not None:
            msg = "password is set, username must be set"
            raise ValueError(msg)
        return None
    if password is None:
        msg = "username is set, password must be set"
        raise ValueError(msg)
    return username, password


@dataclass(frozen=True, slots=True)
class ConnectionParameters:
    host: str
    port: int
    login: str
    passcode: str = field(repr=False)
    ws_uri_path: str | None = None
    connect_headers: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def unescaped_passcode(self) -> str:
        return unquote(self.passcode)

    @classmethod
    def from_pydantic_multihost_hosts(cls, hosts: list[MultiHostHostLike]) -> list[Self]:
        """Create connection parameters from `pydantic_code.MultiHostUrl.hosts()`.

        .. code-block:: python
            import stompman

            ArtemisDsn = typing.Annotated[
                pydantic_core.MultiHostUrl,
                pydantic.UrlConstraints(
                    host_required=True,
                    allowed_schemes=["tcp"],
                ),
            ]

            async with stompman.Client(
                servers=stompman.ConnectionParameters.from_pydantic_multihost_hosts(
                    ArtemisDsn("tcp://user:pass@host1:61616,host2:61617,host3:61618").hosts()
                    # or: ArtemisDsn("tcp://user1:pass1@host1:61616,user2:pass2@host2:61617,user3:pass@host3:61618").hosts()
                ),
            ):
                ...
        """
        all_hosts: list[tuple[str, int]] = []
        all_credentials: list[tuple[str, str]] = []

        for host in hosts:
            all_hosts.append((_required(host["host"], "host"), _required(host["port"], "port")))
            credentials = _credentials(host)
            if credentials is not None:
                all_credentials.append(credentials)

        match len(all_credentials):
            case value if value == len(all_hosts):
                return [
                    cls(host=host, port=port, login=username, passcode=password)
                    for ((host, port), (username, password)) in zip(all_hosts, all_credentials, strict=True)
                ]
            case 1:
                username, password = all_credentials[0]
                return [cls(host=host, port=port, login=username, passcode=password) for (host, port) in all_hosts]
            case 0:
                msg = "username and password must be set"
                raise ValueError(msg)
            case _:
                msg = "all username-password pairs or only one pair must be set"
                raise ValueError(msg)
