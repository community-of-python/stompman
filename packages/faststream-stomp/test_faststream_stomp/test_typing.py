# pragma: no cover
import typing

if typing.TYPE_CHECKING:
    import faststream
    import faststream_stomp
    import stompman
    from faststream_stomp.opentelemetry import StompTelemetryMiddleware
    from faststream_stomp.prometheus import StompPrometheusMiddleware
    from prometheus_client import CollectorRegistry

    broker = faststream_stomp.StompBroker(
        stompman.Client(servers=[]),
        middlewares=(
            StompTelemetryMiddleware(),
            StompPrometheusMiddleware(registry=CollectorRegistry()),
        ),
    )
    app = faststream.FastStream(broker)
