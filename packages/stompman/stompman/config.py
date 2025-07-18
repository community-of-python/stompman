from dataclasses import dataclass, field
from typing import Self, TypedDict
from urllib.parse import unquote


@dataclass(frozen=True, slots=True)
class Heartbeat:
    will_send_interval_ms: int
    want_to_receive_interval_ms: int

    def to_header(self) -> str:
        return f"{self.will_send_interval_ms},{self.want_to_receive_interval_ms}"

    @classmethod
    def from_header(cls, header: str) -> Self:
        first, second = header.split(",", maxsplit=1)
        return cls(int(first), int(second))


class MultiHostHostLike(TypedDict):
    username: str | None
    password: str | None
    host: str | None
    port: int | None


@dataclass(frozen=True, slots=True)
class ConnectionParameters:
    host: str
    port: int
    login: str
    passcode: str = field(repr=False)

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
            if host["host"] is None:
                msg = "host must be set"
                raise ValueError(msg)
            if host["port"] is None:
                msg = "port must be set"
                raise ValueError(msg)
            all_hosts.append((host["host"], host["port"]))

            username, password = host["username"], host["password"]
            if username is None:
                if password is not None:
                    msg = "password is set, username must be set"
                    raise ValueError(msg)
            elif password is None:
                msg = "username is set, password must be set"
                raise ValueError(msg)
            else:
                all_credentials.append((username, password))

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
