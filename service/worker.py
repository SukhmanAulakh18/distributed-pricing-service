"""Queue worker: prices book slices (intraday) and exposure blocks (batch).

Run: python -m service.worker
"""
import json
import logging
import os
import signal
import socket
import time
from collections import OrderedDict

import redis
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from opentelemetry.trace import SpanKind, Status, StatusCode

from . import exposure, logging_setup, taskqueue, tracing
from .cache import PriceCache
from .pricing import HOSTNAME, PRICER_THREADS, price_task
from .store import build_store

log = logging.getLogger("pricer.worker")

TASKS = Counter("worker_tasks_total", "Tasks processed", ["kind", "outcome"])
TASK_SECONDS = Histogram("worker_task_seconds", "Per-task processing time", ["kind"],
                         buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30))
RECLAIMED = Counter("worker_reclaimed_total", "Tasks reclaimed from dead consumers")
BACKLOG = Gauge("pricing_queue_backlog", "Stream backlog", ["stream", "kind"])

RECLAIM_EVERY_S = 5.0


class Worker:
    def __init__(self, r: redis.Redis, cache: PriceCache, store, consumer: str):
        self.r = r
        self.cache = cache
        self.store = store
        self.consumer = consumer
        self.stopping = False
        # Exposure payloads by job id: fetched once per job, not per block.
        self.payloads: OrderedDict[str, dict] = OrderedDict()

    def _payload(self, job: str) -> dict:
        if job not in self.payloads:
            raw = taskqueue.payload(self.r, job)
            if raw is None:
                raise ValueError("job payload expired or missing")
            self.payloads[job] = json.loads(raw)
            if len(self.payloads) > 4:
                self.payloads.popitem(last=False)
        return self.payloads[job]

    def handle(self, stream: str, message_id: str, fields: dict) -> None:
        try:
            job, idx, kind = fields[b"job"].decode(), int(fields[b"idx"]), fields[b"kind"].decode()
        except (KeyError, ValueError):
            # Not one of ours (e.g. added by hand). With no job id there is
            # nowhere to report a result, so log, ack and move on rather than
            # crash: unacked, it would be reclaimed and crash the next worker.
            log.error("malformed task message, dropping it", extra={"stream": stream, "id": message_id})
            taskqueue.ack(self.r, stream, message_id)
            TASKS.labels("unknown", "malformed").inc()
            return
        t0 = time.perf_counter()
        blob = None
        parent = tracing.extract(fields.get(b"traceparent", b"").decode())
        with tracing.tracer.start_as_current_span(f"task.{kind}", context=parent, kind=SpanKind.CONSUMER,
                                                  attributes={"job.id": job, "task.idx": idx, "stream": stream}) as span:
            try:
                task = json.loads(fields[b"task"])
                if kind == "portfolio":
                    span.set_attribute("task.trades", len(task["trades"]))
                    result = price_task(task, self.cache, self.store, job_id=job)
                elif kind == "exposure":
                    span.set_attribute("task.scenarios", task["count"])
                    result, blob = exposure.run_block(self._payload(job), task["start"], task["count"],
                                                      PRICER_THREADS)
                else:
                    raise ValueError(f"unknown task kind {kind!r}")
                outcome = "ok"
            except Exception as e:
                log.exception("task failed", extra={"job_id": job, "idx": idx, "kind": kind})
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                result, outcome = {"error": str(e)}, "error"
        result["priced_by"] = HOSTNAME
        result["ms"] = round((time.perf_counter() - t0) * 1e3, 2)
        result["finished_at"] = time.time()
        # Write the result before acking: dying between the two means the task
        # is reclaimed and recomputed, which is harmless. The reverse order
        # could lose a result.
        taskqueue.complete(self.r, job, idx, result, blob)
        taskqueue.ack(self.r, stream, message_id)
        TASKS.labels(kind, outcome).inc()
        TASK_SECONDS.labels(kind).observe(time.perf_counter() - t0)

    def fail_poison(self, stream: str, message_id: str, fields: dict) -> None:
        if b"job" not in fields or b"idx" not in fields:
            return self.handle(stream, message_id, fields)  # malformed: handle() drops it
        job, idx = fields[b"job"].decode(), int(fields[b"idx"])
        log.error("task exceeded max deliveries, failing it", extra={"job_id": job, "idx": idx})
        taskqueue.complete(self.r, job, idx, {"error": "exceeded max deliveries", "priced_by": HOSTNAME})
        taskqueue.ack(self.r, stream, message_id)
        TASKS.labels(fields.get(b"kind", b"unknown").decode(), "poison").inc()

    def reclaim_once(self) -> None:
        retry, poison = taskqueue.reclaim(self.r, self.consumer)
        for s, mid, fields in poison:
            self.fail_poison(s, mid, fields)
        for s, mid, fields in retry:
            RECLAIMED.inc()
            self.handle(s, mid, fields)

    def run_once(self, block_ms: int = 1000) -> int:
        with tracing.quiet():  # idle polling is not worth a trace
            msgs = taskqueue.read(self.r, self.consumer, block_ms=block_ms)
        for s, mid, fields in msgs:
            self.handle(s, mid, fields)
        return len(msgs)

    def run(self) -> None:
        taskqueue.ensure_groups(self.r)
        last_reclaim = 0.0
        log.info("worker started", extra={"consumer": self.consumer, "queue_mode": taskqueue.mode(),
                                          "streams": taskqueue.worker_streams()})
        while not self.stopping:
            try:
                if time.monotonic() - last_reclaim > RECLAIM_EVERY_S:
                    self.reclaim_once()
                    with tracing.quiet():
                        backlog = taskqueue.backlog(self.r)
                    for s, b in backlog.items():
                        BACKLOG.labels(s, "lag").set(b["lag"])
                        BACKLOG.labels(s, "pending").set(b["pending"])
                    last_reclaim = time.monotonic()
                self.run_once()
            except redis.RedisError as e:
                log.warning("redis error, backing off", extra={"error": str(e)})
                time.sleep(1)
        if hasattr(self.store, "flush"):
            self.store.flush()
        log.info("worker stopped cleanly", extra={"consumer": self.consumer})


def main() -> None:
    logging_setup.configure()
    tracing.setup("pricing-worker")
    start_http_server(int(os.getenv("METRICS_PORT", "9100")))
    r = redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=5, socket_connect_timeout=2)
    cache = PriceCache(r, ttl_seconds=int(os.getenv("CACHE_TTL_S", "30")))
    w = Worker(r, cache, build_store(), consumer=f"{socket.gethostname()}-{os.getpid()}")

    # On SIGTERM (pod shutdown) finish the current task, then exit. Anything
    # still unacked is reclaimed by another worker.
    def stop(*_):
        w.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    w.run()


if __name__ == "__main__":
    main()
