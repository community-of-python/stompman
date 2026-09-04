from typing import TypedDict

import faker
import pytest
import stompman
from polyfactory.factories.typed_dict_factory import TypedDictFactory
from stompman.config import MultiHostHostLike


class StrictMultiHostHostLike(TypedDict):
    username: str
    password: str
    host: str
    port: int


class StrictMultiHostHostLikeFactory(TypedDictFactory[StrictMultiHostHostLike]): ...


class MultiHostHostLikeFactory(TypedDictFactory[MultiHostHostLike]): ...


class TestConnectionParametersFromPydanticMultiHostHosts:
    def test_ok_with_one_credentials(self, faker: faker.Faker) -> None:
        hosts: list[MultiHostHostLike] = [
            {"host": "host1", "port": 1, "username": None, "password": None},
            {"host": "host2", "port": 2, "username": None, "password": None},
            {"host": "host3", "port": 3, "username": None, "password": None},
            {"host": "host4", "port": 4, "username": None, "password": None},
        ]
        host_with_credentials = faker.pyint(min_value=0, max_value=3)
        hosts[host_with_credentials]["username"] = "lev"
        hosts[host_with_credentials]["password"] = "pass"  # ruff: ignore[hardcoded-password-string]

        result = stompman.ConnectionParameters.from_pydantic_multihost_hosts(hosts)

        assert result == [
            stompman.ConnectionParameters("host1", 1, "lev", "pass"),
            stompman.ConnectionParameters("host2", 2, "lev", "pass"),
            stompman.ConnectionParameters("host3", 3, "lev", "pass"),
            stompman.ConnectionParameters("host4", 4, "lev", "pass"),
        ]

    def test_ok_with_all_credentials(self, faker: faker.Faker) -> None:
        hosts: list[MultiHostHostLike] = [
            {"host": "host1", "port": 1, "username": "user1", "password": "pass1"},
            {"host": "host2", "port": 2, "username": "user2", "password": "pass2"},
            {"host": "host3", "port": 3, "username": "user3", "password": "pass3"},
            {"host": "host4", "port": 4, "username": "user4", "password": "pass4"},
        ]

        result = stompman.ConnectionParameters.from_pydantic_multihost_hosts(hosts)

        assert result == [
            stompman.ConnectionParameters("host1", 1, "user1", "pass1"),
            stompman.ConnectionParameters("host2", 2, "user2", "pass2"),
            stompman.ConnectionParameters("host3", 3, "user3", "pass3"),
            stompman.ConnectionParameters("host4", 4, "user4", "pass4"),
        ]

    def test_no_host_or_port_or_both(self, faker: faker.Faker) -> None:
        cases: list[MultiHostHostLike] = [
            {"host": None, "port": faker.pyint(), "username": faker.pystr(), "password": faker.pystr()},
            {"host": faker.pystr(), "port": None, "username": faker.pystr(), "password": faker.pystr()},
            {"host": None, "port": None, "username": faker.pystr(), "password": faker.pystr()},
        ]

        for host in cases:
            with pytest.raises(ValueError, match="must be set"):
                stompman.ConnectionParameters.from_pydantic_multihost_hosts([host])

    def test_no_username(self, faker: faker.Faker) -> None:
        hosts: list[MultiHostHostLike] = [
            {"host": faker.pystr(), "port": faker.pyint(), "username": None, "password": faker.pystr()},
            {"host": faker.pystr(), "port": faker.pyint(), "username": faker.pystr(), "password": faker.pystr()},
        ]

        with pytest.raises(ValueError, match="username must be set"):
            stompman.ConnectionParameters.from_pydantic_multihost_hosts(hosts)

    def test_no_password(self, faker: faker.Faker) -> None:
        hosts: list[MultiHostHostLike] = [
            {"host": faker.pystr(), "port": faker.pyint(), "username": faker.pystr(), "password": None},
            {"host": faker.pystr(), "port": faker.pyint(), "username": faker.pystr(), "password": faker.pystr()},
        ]

        with pytest.raises(ValueError, match="password must be set"):
            stompman.ConnectionParameters.from_pydantic_multihost_hosts(hosts)

    def test_no_credentials(self, faker: faker.Faker) -> None:
        cases: list[MultiHostHostLike] = [
            {"host": faker.pystr(), "port": faker.pyint(), "username": None, "password": None},
            {"host": faker.pystr(), "port": faker.pyint(), "username": None, "password": None},
        ]

        for host in cases:
            with pytest.raises(ValueError, match="username and password must be set"):
                stompman.ConnectionParameters.from_pydantic_multihost_hosts([host])

    def test_multiple_credentials(self, faker: faker.Faker) -> None:
        hosts: list[MultiHostHostLike] = [
            {"host": faker.pystr(), "port": faker.pyint(), "username": None, "password": None},
            {"host": faker.pystr(), "port": faker.pyint(), "username": faker.pystr(), "password": faker.pystr()},
            {"host": faker.pystr(), "port": faker.pyint(), "username": faker.pystr(), "password": faker.pystr()},
        ]

        with pytest.raises(ValueError, match="all username-password pairs or only one pair must be set"):
            stompman.ConnectionParameters.from_pydantic_multihost_hosts(hosts)
