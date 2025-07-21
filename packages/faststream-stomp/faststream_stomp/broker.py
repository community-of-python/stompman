import asyncio
import logging
import types
import typing
from collections.abc import Iterable, Sequence
from typing import (
    TYPE_CHECKING,
    cast,
)

# TODO: simplify imports  # noqa: FIX002, TD002, TD003
import anyio
import stompman
from fast_depends.dependencies import Dependant
from faststream._internal.basic_types import AnyDict, LoggerProto, SendableMessage
from faststream._internal.broker import BrokerUsecase
from faststream._internal.broker.registrator import Registrator
from faststream._internal.configs import (
    BrokerConfig,
)
from faststream._internal.constants import EMPTY
from faststream._internal.context import ContextRepo
from faststream._internal.di import FastDependsConfig
from faststream._internal.logger import (
    DefaultLoggerStorage,
    make_logger_state,
)
from faststream._internal.logger.logging import get_broker_logger
from faststream._internal.types import (
    BrokerMiddleware,
    CustomCallable,
)
from faststream.response.publish_type import PublishType
from faststream.security import BaseSecurity
from faststream.specification.schema import BrokerSpec
from faststream.specification.schema.extra import Tag, TagDict

from faststream_stomp.configs import StompBrokerConfig
from faststream_stomp.publisher import StompPublishCommand, StompPublisher
from faststream_stomp.registrator import StompRegistrator

if TYPE_CHECKING:
    from faststream_stomp.subscriber import StompSubscriber


class StompSecurity(BaseSecurity):
    def __init__(self) -> None:
        self.ssl_context = None
        self.use_ssl = False

    def get_requirement(self) -> list[AnyDict]:  # noqa: PLR6301
        return [{"user-password": []}]

    def get_schema(self) -> dict[str, dict[str, str]]:  # noqa: PLR6301
        return {"user-password": {"type": "userPassword"}}


class StompParamsStorage(DefaultLoggerStorage):
    __max_msg_id_ln = 10
    _max_channel_name = 4

    def get_logger(self, *, context: ContextRepo) -> LoggerProto:
        if logger := self._get_logger_ref():
            return logger
        logger = get_broker_logger(
            name="stomp",
            default_context={"destination": "", "message_id": ""},
            message_id_ln=self.__max_msg_id_ln,
            fmt=(
                "%(asctime)s %(levelname)-8s - "
                f"%(destination)-{self._max_channel_name}s | "
                f"%(message_id)-{self.__max_msg_id_ln}s "
                "- %(message)s"
            ),
            context=context,
            log_level=self.logger_log_level,
        )
        self._logger_ref.add(logger)
        return logger


class StompBroker(StompRegistrator, BrokerUsecase[stompman.MessageFrame, stompman.Client, BrokerConfig]):
    _subscribers: list["StompSubscriber"]  # type: ignore[assignment]
    _publishers: list["StompPublisher"]  # type: ignore[assignment]

    def __init__(
        self,
        client: stompman.Client,
        *,
        decoder: CustomCallable | None = None,
        parser: CustomCallable | None = None,
        dependencies: Iterable[Dependant] = (),
        middlewares: Sequence[BrokerMiddleware[stompman.MessageFrame, StompPublishCommand]] = (),
        graceful_timeout: float | None = None,
        routers: Sequence[Registrator[stompman.MessageFrame]] = (),
        logger: LoggerProto | None = EMPTY,
        log_level: int = logging.INFO,
        apply_types: bool = True,
        # AsyncAPI args
        description: str | None = None,
        tags: Iterable[Tag | TagDict] = (),
    ) -> None:
        broker_config = StompBrokerConfig(
            broker_middlewares=cast("Sequence[BrokerMiddleware]", middlewares),
            broker_parser=parser,
            broker_decoder=decoder,
            logger=make_logger_state(
                logger=logger,
                log_level=log_level,
                default_storage_cls=StompParamsStorage,  # type: ignore[type-abstract]
            ),
            fd_config=FastDependsConfig(use_fastdepends=apply_types),
            broker_dependencies=dependencies,
            graceful_timeout=graceful_timeout,
            extra_context={"broker": self},
            client=client,
        )
        specification = BrokerSpec(
            url=[f"{one_server.host}:{one_server.port}" for one_server in broker_config.client.servers],
            protocol="STOMP",
            protocol_version="1.2",
            description=description,
            tags=tags,
            security=StompSecurity(),
        )

        super().__init__(config=broker_config, specification=specification, routers=routers)
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

    async def publish(
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
