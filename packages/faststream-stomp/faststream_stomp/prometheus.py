from __future__ import annotations

from typing import TYPE_CHECKING

import stompman
from faststream._internal.constants import EMPTY
from faststream.prometheus import ConsumeAttrs, MetricsSettingsProvider
from faststream.prometheus.middleware import PrometheusMiddleware

from faststream_stomp.models import StompPublishCommand

if TYPE_CHECKING:
    from collections.abc import Sequence

    from faststream import StreamMessage
    from prometheus_client import CollectorRegistry

__all__ = ["StompMetricsSettingsProvider", "StompPrometheusMiddleware"]


class StompMetricsSettingsProvider(MetricsSettingsProvider[stompman.MessageFrame, StompPublishCommand]):
    messaging_system = "stomp"

    def get_consume_attrs_from_message(self, msg: StreamMessage[stompman.MessageFrame]) -> ConsumeAttrs:  # noqa: PLR6301
        return {
            "destination_name": msg.raw_message.headers["destination"],
            "message_size": len(msg.body),
            "messages_count": 1,
        }

    def get_publish_destination_name_from_cmd(self, cmd: StompPublishCommand) -> str:  # noqa: PLR6301
        return cmd.destination


class StompPrometheusMiddleware(PrometheusMiddleware[StompPublishCommand, stompman.MessageFrame]):
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
