import asyncio
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Unpack

import anyio
import stompman

# from faststream._internal.basic_types import EMPTY, AnyDict, Decorator, LoggerProto, SendableMessage
from faststream._internal.basic_types import AnyDict, SendableMessage
from faststream._internal.broker.broker import BrokerUsecase
from faststream._internal.broker.registrator import Registrator
from faststream._internal.configs import BrokerConfig
from faststream.security import BaseSecurity
from faststream.specification.schema import BrokerSpec
from typing_extensions import Doc

from faststream_stomp.publisher import StompProducer, StompPublisher
from faststream_stomp.registrator import StompRegistrator
from faststream_stomp.subscriber import StompLogContext, StompSubscriber


@dataclass(kw_only=True)
class StompBrokerConfig(BrokerConfig):
    client: stompman.Client


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
    _subscribers: Mapping[int, StompSubscriber]
    _publishers: Mapping[int, StompPublisher]
    __max_msg_id_ln = 10
    _max_channel_name = 4

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
        super().__init__(
            config=config,
            specification=specification,
            routers=routers,
        )
        self._attempted_to_connect = False

    async def start(self) -> None:
        await super().start()

        for handler in self._subscribers.values():
            self._log(f"`{handler.call_name}` waiting for messages", extra=handler.get_log_context(None))
            await handler.start()

    async def _connect(self, client: stompman.Client) -> stompman.Client:  # type: ignore[override]
        if self._attempted_to_connect:
            return client
        self._attempted_to_connect = True
        self._producer = StompProducer(client)
        await client.__aenter__()
        client._listen_task.add_done_callback(_handle_listen_task_done)  # noqa: SLF001
        return client

    async def _close(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: types.TracebackType | None = None,
    ) -> None:
        if self._connection:
            await self._connection.__aexit__(exc_type, exc_val, exc_tb)
        return await super()._close(exc_type, exc_val, exc_tb)

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

    def get_fmt(self) -> str:
        # `StompLogContext`
        return (
            "%(asctime)s %(levelname)-8s - "
            f"%(destination)-{self._max_channel_name}s | "
            f"%(message_id)-{self.__max_msg_id_ln}s "
            "- %(message)s"
        )

    def _setup_log_context(self, **log_context: Unpack[StompLogContext]) -> None: ...  # type: ignore[override]

    @property
    def _subscriber_setup_extra(self) -> "AnyDict":
        return {**super()._subscriber_setup_extra, "client": self._connection}

    async def publish(  # type: ignore[override]
        self,
        message: SendableMessage,
        destination: str,
        *,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        await super().publish(
            message,
            producer=self._producer,
            correlation_id=correlation_id,
            destination=destination,
            headers=headers,
        )

    async def request(  # type: ignore[override]
        self,
        msg: Any,  # noqa: ANN401
        *,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:  # noqa: ANN401
        return await super().request(msg, producer=self._producer, correlation_id=correlation_id, headers=headers)


def _handle_listen_task_done(listen_task: asyncio.Task[None]) -> None:
    # Not sure how to test this. See https://github.com/community-of-python/stompman/pull/117#issuecomment-2983584449.
    task_exception = listen_task.exception()
    if isinstance(task_exception, ExceptionGroup) and isinstance(
        task_exception.exceptions[0], stompman.FailedAllConnectAttemptsError
    ):
        raise SystemExit(1)
