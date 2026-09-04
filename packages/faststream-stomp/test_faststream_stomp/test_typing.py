# pragma: no cover
import typing

if typing.TYPE_CHECKING:
    import faststream
    import faststream_stomp
    import stompman
    from faststream_stomp.opentelemetry import StompTelemetryMiddleware
    from faststream_stomp.prometheus import StompPrometheusMiddleware
    from prometheus_client import CollectorRegistry
    from stompman.core.config import RuntimeConfig
    from stompman.core.runtime import Runtime

    broker = faststream_stomp.StompBroker(
        stompman.Client(servers=[]),
        middlewares=(
            StompTelemetryMiddleware(),
            StompPrometheusMiddleware(registry=CollectorRegistry()),
        ),
    )
    app = faststream.FastStream(broker)
    native_broker = faststream_stomp.StompBroker(RuntimeConfig(servers=[]))
    explicit_runtime_broker = faststream_stomp.StompBroker(Runtime(RuntimeConfig(servers=[])))
    servers_broker = faststream_stomp.StompBroker(servers=[])
    native_app = faststream.FastStream(native_broker)

    async def check_add_content_length_typing() -> None:
        await broker.publish("message", "destination", add_content_length=False)
        await broker.publish_batch("first", "second", destination="destination", add_content_length=False)

        publisher = broker.publisher("destination", add_content_length=False)
        await publisher.publish("message", add_content_length=True)
        await publisher.publish_batch("first", "second", add_content_length=True)

        broker.subscriber("destination", reply_add_content_length=False)
