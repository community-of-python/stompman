from __future__ import annotations

from typing import TYPE_CHECKING

import stompman
from faststream.prometheus import ConsumeAttrs, MetricsSettingsProvider
from faststream.prometheus.middleware import BasePrometheusMiddleware

if TYPE_CHECKING:
    from collections.abc import Sequence

    from faststream.message import StreamMessage
    from prometheus_client import CollectorRegistry

    from faststream_stomp.publisher import StompProducerPublishKwargs

__all__ = ["StompMetricsSettingsProvider", "StompPrometheusMiddleware"]


class StompMetricsSettingsProvider(MetricsSettingsProvider[stompman.MessageFrame]):
    messaging_system = "stomp"

    def get_consume_attrs_from_message(self, msg: StreamMessage[stompman.MessageFrame]) -> ConsumeAttrs:  # noqa: PLR6301
        return {
            "destination_name": msg.raw_message.headers["destination"],
            "message_size": len(msg.body),
            "messages_count": 1,
        }

    def get_publish_destination_name_from_kwargs(self, kwargs: StompProducerPublishKwargs) -> str:  # type: ignore[override]  # noqa: PLR6301
        return kwargs["destination"]


class StompPrometheusMiddleware(BasePrometheusMiddleware):
    def __init__(
        self,
        *,
        registry: CollectorRegistry,
        app_name: str = EMPTY,
        metrics_prefix: str = "faststream",
        received_messages_size_buckets: Sequence[float] | None = None,
    ) -> None:
        super().__init__(
            settings_provider_factory=lambda _: StompMetricsSettingsProvider(),
            registry=registry,
            app_name=app_name,
            metrics_prefix=metrics_prefix,
            received_messages_size_buckets=received_messages_size_buckets,
        )
