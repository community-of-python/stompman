import asyncio
import types
import typing
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated

import anyio
import stompman
from faststream._internal.basic_types import AnyDict, SendableMessage
from faststream._internal.broker.broker import BrokerUsecase
from faststream._internal.broker.registrator import Registrator
from faststream.response.publish_type import PublishType
from faststream.security import BaseSecurity
from faststream.specification.schema import BrokerSpec
from typing_extensions import Doc

from faststream_stomp.configs import StompBrokerConfig
from faststream_stomp.publisher import StompPublishCommand
from faststream_stomp.registrator import StompRegistrator


class StompSecurity(BaseSecurity):
    def __init__(self) -> None:
        self.ssl_context = None
        self.use_ssl = False

    def get_requirement(self) -> list[AnyDict]:  # noqa: PLR6301
        return [{"user-password": []}]

    def get_schema(self) -> dict[str, dict[str, str]]:  # noqa: PLR6301
        return {"user-password": {"type": "userPassword"}}


@dataclass(kw_only=True)
class StompBrokerSpec(BrokerSpec):
    url: Annotated[list[str], Doc("URLs of servers will be inferred from client if not specified")] = field(
        default_factory=list
    )
    protocol: str | None = "STOMP"
    protocol_version: str | None = "1.2"
    security: BaseSecurity | None = field(default_factory=StompSecurity)


class StompBroker(StompRegistrator, BrokerUsecase[stompman.MessageFrame, stompman.Client, StompBrokerConfig]):
    def __init__(
        self,
        *,
        config: StompBrokerConfig,
        specification: StompBrokerSpec,
        routers: Sequence[Registrator[stompman.MessageFrame]],
    ) -> None:
        specification.url = specification.url or [
            f"{one_server.host}:{one_server.port}" for one_server in config.client.servers
        ]
        super().__init__(config=config, specification=specification, routers=routers)
        self._attempted_to_connect = False

    async def _connect(self, client: stompman.Client) -> stompman.Client:  # type: ignore[override]
        if self._attempted_to_connect:
            return client
        self._attempted_to_connect = True
        await client.__aenter__()
        client._listen_task.add_done_callback(_handle_listen_task_done)  # noqa: SLF001
        return client

    async def stop(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: types.TracebackType | None = None,
    ) -> None:
        if self._connection:
            await self._connection.__aexit__(exc_type, exc_val, exc_tb)
        return await super().stop(exc_type, exc_val, exc_tb)

    async def ping(self, timeout: float | None = None) -> bool:
        sleep_time = (timeout or 10) / 10
        with anyio.move_on_after(timeout) as cancel_scope:
            if self._connection is None:
                return False

            while True:
                if cancel_scope.cancel_called:
                    return False

                if self._connection.is_alive():
                    return True

                await anyio.sleep(sleep_time)  # pragma: no cover

        return False  # pragma: no cover

    async def publish(  # type: ignore[override]
        self,
        message: SendableMessage,
        destination: str,
        *,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        publish_command = StompPublishCommand(
            message,
            _publish_type=PublishType.PUBLISH,
            destination=destination,
            correlation_id=correlation_id,
            headers=headers,
        )
        return typing.cast("None", self._basic_publish(publish_command, producer=self._producer))

    async def request(  # type: ignore[override]
        self,
        message: SendableMessage,
        destination: str,
        *,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        publish_command = StompPublishCommand(
            message,
            _publish_type=PublishType.REQUEST,
            destination=destination,
            correlation_id=correlation_id,
            headers=headers,
        )
        return typing.cast("None", self._basic_request(publish_command, producer=self._producer))


def _handle_listen_task_done(listen_task: asyncio.Task[None]) -> None:
    # Not sure how to test this. See https://github.com/community-of-python/stompman/pull/117#issuecomment-2983584449.
    task_exception = listen_task.exception()
    if isinstance(task_exception, ExceptionGroup) and isinstance(
        task_exception.exceptions[0], stompman.FailedAllConnectAttemptsError
    ):
        raise SystemExit(1)
