"""Distributed tracing with OpenTelemetry.

One trace follows a request from the API, through the Redis Stream (the trace
context travels in each task message), into whichever worker prices each
task, including its Redis and Cassandra calls. Enabled when
OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise every call here is a no-op.
"""
import os
import socket
from contextlib import contextmanager

from opentelemetry import context, propagate, trace

tracer = trace.get_tracer("pricing")


def setup(service_name: str, app=None) -> None:
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.cassandra import CassandraInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", service_name),
                                  "service.instance.id": socket.gethostname()}),
        # Head sampling at the root; workers follow the API's decision, so a
        # sampled book is traced end to end and an unsampled one costs nothing.
        sampler=ParentBased(TraceIdRatioBased(float(os.getenv("OTEL_SAMPLE_RATIO", "1.0")))))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    RedisInstrumentor().instrument()
    CassandraInstrumentor().instrument()
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics", exclude_spans=["receive", "send"])


def inject() -> str:
    """The current trace context as a W3C traceparent, to put in a message."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent", "")


def extract(traceparent: str | None):
    """Context to continue a trace from a message's traceparent."""
    return propagate.extract({"traceparent": traceparent}) if traceparent else context.get_current()


def trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else "-"


@contextmanager
def quiet():
    """No auto-instrumented spans inside: for polling loops (a worker's idle
    XREADGROUP every second, the API polling a book's progress) and the
    price-log thread, which would otherwise bury the useful traces."""
    from opentelemetry.instrumentation.utils import suppress_instrumentation

    with suppress_instrumentation():
        yield
