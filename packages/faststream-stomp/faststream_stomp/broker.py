import logging
import types
import typing
from collections.abc import Iterable, Sequence
from typing import Any

import anyio
import stompman
from fast_depends.dependencies import Dependant
from faststream import BaseMiddleware, ContextRepo, PublishType
from faststream._internal.basic_types import LoggerProto, SendableMessage
from faststream._internal.broker import BrokerUsecase
from faststream._internal.broker.registrator import Registrator
from faststream._internal.configs import BrokerConfig
from faststream._internal.constants import EMPTY
from faststream._internal.di import FastDependsConfig
from faststream._internal.logger import DefaultLoggerStorage, make_logger_state
from faststream._internal.logger.logging import get_broker_logger
from faststream._internal.types import BrokerMiddleware, CustomCallable
from faststream.security import BaseSecurity
from faststream.specification.schema import BrokerSpec
from faststream.specification.schema.extra import Tag, TagDict
from stompman._compat import server_from_legacy
from stompman.core.config import RuntimeConfig
from stompman.core.runtime import Runtime
from stompman.core.transport import TransportFactory, connect_tcp

from faststream_stomp._client import adapt_client
from faststream_stomp.models import BrokerConfigWithStompClient, StompPublishCommand
from faststream_stomp.publisher import StompProducer, StompPublisher
from faststream_stomp.registrator import StompRegistrator
from faststream_stomp.subscriber import StompSubscriber


class StompSecurity(BaseSecurity):
    def __init__(self) -> None:
        self.ssl_context = None
        self.use_ssl = False

    def get_requirement(self) -> list[dict[str, Any]]:  # ruff: ignore[no-self-use]
        return [{"user-password": []}]

    def get_schema(self) -> dict[str, dict[str, str]]:  # ruff: ignore[no-self-use]
        return {"user-password": {"type": "userPassword"}}


class StompParamsStorage(DefaultLoggerStorage):
    __max_msg_id_ln = -1
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


class StompBroker(
    StompRegistrator,
    BrokerUsecase[
        stompman.MessageFrame,
        stompman.Client | Runtime,
        BrokerConfig,  # Using BrokerConfig to avoid typing issues when passing broker to FastStream app
    ],
):
    _subscribers: list[StompSubscriber]  # type: ignore[assignment]
    _publishers: list[StompPublisher]  # type: ignore[assignment]

    def __init__(
        self,
        client: stompman.Client | Runtime | RuntimeConfig | None = None,
        *,
        servers: list[stompman.ConnectionParameters] | None = None,
        transport_factory: TransportFactory = connect_tcp,
        decoder: CustomCallable | None = None,
        parser: CustomCallable | None = None,
        dependencies: Iterable[Dependant] = (),
        middlewares: Sequence[type[BaseMiddleware] | BrokerMiddleware[stompman.MessageFrame, StompPublishCommand]] = (),
        graceful_timeout: float | None = 15.0,
        routers: Sequence[Registrator[stompman.MessageFrame]] = (),
        add_content_length: bool = True,
        # Logging args
        logger: LoggerProto | None = EMPTY,
        log_level: int = logging.INFO,
        # FastDepends args
        apply_types: bool = True,
        # AsyncAPI args
        description: str | None = None,
        tags: Iterable[Tag | TagDict] = (),
    ) -> None:
        if servers is not None:
            if client is not None:
                msg = "provide either client/runtime configuration or servers, not both"
                raise TypeError(msg)
            client = RuntimeConfig(tuple(server_from_legacy(server) for server in servers))
        if client is None:
            msg = "provide a runtime, runtime configuration, Client, or servers"
            raise TypeError(msg)
        connection = (
            Runtime(client, transport_factory=transport_factory) if isinstance(client, RuntimeConfig) else client
        )
        fd_config = FastDependsConfig(use_fastdepends=apply_types)
        broker_config = BrokerConfigWithStompClient(
            broker_middlewares=middlewares,  # type: ignore[arg-type]
            broker_parser=parser,
            broker_decoder=decoder,
            logger=make_logger_state(
                logger=logger,
                log_level=log_level,
                default_storage_cls=StompParamsStorage,  # type: ignore[type-abstract]
            ),
            fd_config=fd_config,
            broker_dependencies=dependencies,
            graceful_timeout=graceful_timeout,
            extra_context={"broker": self},
            producer=StompProducer(
                client=connection,
                serializer=fd_config._serializer,
                add_content_length=add_content_length,
            ),
            client=connection,
        )
        specification = BrokerSpec(
            url=[
                f"{one_server.host}:{one_server.port}"
                for one_server in (connection.config.servers if isinstance(connection, Runtime) else connection.servers)
            ],
            protocol="STOMP",
            protocol_version="1.2",
            description=description,
            tags=tags,
            security=StompSecurity(),
        )

        super().__init__(config=broker_config, specification=specification, routers=routers)
        self._stopping = False

    @property
    def runtime(self) -> Runtime:
        client = self.config.broker_config.client
        return client if isinstance(client, Runtime) else client.core

    async def _connect(self) -> stompman.Client | Runtime:
        client = self.config.broker_config.client
        await adapt_client(client).open()
        self._stopping = False
        return client

    async def start(self) -> None:
        if self.running:
            return
        await self.connect()
        try:
            await super().start()
        except BaseException as error:
            await self.stop(type(error), error, error.__traceback__)
            raise

    async def stop(
        self,
        exc_type: type[BaseException] | None = None,
        exc_val: BaseException | None = None,
        exc_tb: types.TracebackType | None = None,
    ) -> None:
        self._stopping = True
        try:
            await super().stop(exc_type, exc_val, exc_tb)
        except BaseException as error:
            exc_type, exc_val, exc_tb = type(error), error, error.__traceback__
            raise
        finally:
            try:
                if self._connection is not None:
                    await adapt_client(self._connection).close(exc_type, exc_val, exc_tb)
            finally:
                self._connection = None
                self.running = False

    async def ping(self, timeout: float | None = None) -> bool:
        # broker can be stuck in stopping state
        if self._stopping:
            return False

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
        add_content_length: bool | None = None,
    ) -> None:
        publish_command = StompPublishCommand(
            message,
            _publish_type=PublishType.PUBLISH,
            destination=destination,
            correlation_id=correlation_id,
            headers=headers,
            add_content_length=add_content_length,
        )
        return typing.cast("None", await self._basic_publish(publish_command, producer=self.config.producer))

    async def request(  # type: ignore[override]
        self,
        message: SendableMessage,
        destination: str,
        *,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
        add_content_length: bool | None = None,
    ) -> Any:  # ruff: ignore[any-type]
        publish_command = StompPublishCommand(
            message,
            _publish_type=PublishType.REQUEST,
            destination=destination,
            correlation_id=correlation_id,
            headers=headers,
            add_content_length=add_content_length,
        )
        return await self._basic_request(publish_command, producer=self.config.producer)

    async def publish_batch(  # type: ignore[override]
        self,
        *messages: SendableMessage,
        destination: str,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
        add_content_length: bool | None = None,
    ) -> None:
        publish_command = StompPublishCommand(
            *messages,
            _publish_type=PublishType.PUBLISH,
            destination=destination,
            correlation_id=correlation_id,
            headers=headers,
            add_content_length=add_content_length,
        )
        return typing.cast("None", await self._basic_publish_batch(publish_command, producer=self.config.producer))
