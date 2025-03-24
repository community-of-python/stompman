import logging
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Unpack

import anyio
import stompman
from fast_depends.dependencies import Depends
from faststream.asyncapi.schema import Tag, TagDict
from faststream.broker.core.usecase import BrokerUsecase
from faststream.broker.types import BrokerMiddleware, CustomCallable
from faststream.log.logging import get_broker_logger
from faststream.security import BaseSecurity
from faststream.types import EMPTY, AnyDict, Decorator, LoggerProto, SendableMessage

from faststream_stomp.publisher import StompProducer, StompPublisher
from faststream_stomp.registrator import StompRegistrator
from faststream_stomp.subscriber import StompLogContext, StompSubscriber


class StompSecurity(BaseSecurity):
    def __init__(self) -> None:
        self.ssl_context = None
        self.use_ssl = False

    def get_requirement(self) -> list[AnyDict]:  # noqa: PLR6301
        return [{"user-password": []}]

    def get_schema(self) -> dict[str, dict[str, str]]:  # noqa: PLR6301
        return {"user-password": {"type": "userPassword"}}


class StompBroker(StompRegistrator, BrokerUsecase[stompman.MessageFrame, stompman.Client]):
    _subscribers: Mapping[int, StompSubscriber]
    _publishers: Mapping[int, StompPublisher]
    __max_msg_id_ln = 10
    _max_channel_name = 4

    def __init__(
        self,
        client: stompman.Client,
        *,
        decoder: CustomCallable | None = None,
        parser: CustomCallable | None = None,
        dependencies: Iterable[Depends] = (),
        middlewares: Sequence[BrokerMiddleware[stompman.MessageFrame]] = (),
        graceful_timeout: float | None = 15.0,
        # Logging args
        logger: LoggerProto | None = EMPTY,
        log_level: int = logging.INFO,
        log_fmt: str | None = None,
        # FastDepends args
        apply_types: bool = True,
        validate: bool = True,
        _get_dependant: Callable[..., Any] | None = None,
        _call_decorators: Iterable[Decorator] = (),
        # AsyncAPI kwargs,
        description: str | None = None,
        tags: Iterable[Tag | TagDict] | None = None,
    ) -> None:
        super().__init__(
            client=client,  # **connection_kwargs
            decoder=decoder,
            parser=parser,
            dependencies=dependencies,
            middlewares=middlewares,
            graceful_timeout=graceful_timeout,
            logger=logger,
            log_level=log_level,
            log_fmt=log_fmt,
            apply_types=apply_types,
            validate=validate,
            _get_dependant=_get_dependant,
            _call_decorators=_call_decorators,
            protocol="STOMP",
            protocol_version="1.2",
            description=description,
            tags=tags,
            asyncapi_url=[f"{one_server.host}:{one_server.port}" for one_server in client.servers],
            security=StompSecurity(),
            default_logger=get_broker_logger(
                name="stomp", default_context={"channel": ""}, message_id_ln=self.__max_msg_id_ln
            ),
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
        return await client.__aenter__()

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
