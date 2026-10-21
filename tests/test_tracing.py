import pytest
import redis
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from service import taskqueue
from service.cache import PriceCache

from test_portfolio import book

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def provider():
    # The global provider can be set once per process; spans are cleared per test.
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        p = TracerProvider()
        p.add_span_processor(SimpleSpanProcessor(EXPORTER))
        trace.set_tracer_provider(p)


def test_book_trace_continues_into_every_worker(make_client, r, workers):
    EXPORTER.clear()
    workers(("w1", "w2"))
    with make_client(cache=PriceCache(r), queue=r) as c:
        assert c.post("/portfolio/price", json={**book(6), "timeout_s": 20}).json()["n_failed"] == 0
    spans = EXPORTER.get_finished_spans()
    submit = next(s for s in spans if s.name == "book.submit")
    tasks = [s for s in spans if s.name == "task.portfolio"]
    assert len(tasks) == 2  # 6 trades, task_size 3
    for t in tasks:
        assert t.context.trace_id == submit.context.trace_id  # one trace, across processes' worth of hops
        assert t.parent.span_id == submit.context.span_id
    # One span per task for its Monte Carlo trades, not one per trade.
    batches = [s for s in spans if s.name == "pricer.mc_batch" and s.context.trace_id == submit.context.trace_id]
    assert len(batches) == 2 and sum(s.attributes["trades"] for s in batches) > 0
    assert not any(s.name == "pricer.compute" and s.context.trace_id == submit.context.trace_id for s in spans)


def test_dedicated_pools_read_only_their_stream(r, monkeypatch):
    monkeypatch.setenv("WORKER_STREAMS", "batch")
    taskqueue.ensure_groups(r)
    taskqueue.submit(r, "portfolio", "intraday", [{"n": 1}], {})
    assert taskqueue.read(r, "batch-worker", block_ms=10) == []  # intraday is not ours
    taskqueue.submit(r, "exposure", "batch", [{"n": 2}], {})
    (stream, _, _), = taskqueue.read(r, "batch-worker", block_ms=10)
    assert stream == taskqueue.BATCH


def test_book_wait_rides_out_a_redis_restart(monkeypatch):
    calls = iter([redis.ConnectionError("restarting"), redis.ConnectionError("restarting"), 3])

    def done(r, job_id):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(taskqueue, "done", done)
    monkeypatch.setattr(taskqueue.time, "sleep", lambda s: None)
    assert taskqueue.wait(None, "job", 3, timeout_s=5) is True
