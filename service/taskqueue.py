"""Scatter/gather over Redis Streams, with intraday work ahead of batch work.

The API XADDs one message per task. Workers read through a consumer group,
so each task goes to one worker at a time. A task stays in the group's
pending list until the worker XACKs it; if the worker dies first, another
worker reclaims it with XAUTOCLAIM after CLAIM_IDLE_MS. Results land in a
per-job hash keyed by task index, so a task processed twice just rewrites the
same field with the same (deterministic) value.

Priority: intraday tasks (books priced for e-trading and intraday risk) go to
one stream and batch tasks (exposure runs) to another. A worker serving both
always drains the intraday stream first, so a large batch run can't queue
ahead of a trader waiting on a price. On Kubernetes the two streams also get
their own worker pools (WORKER_STREAMS), each autoscaled by KEDA on its own
stream's lag. QUEUE_MODE=fifo puts everything on one stream instead; it
exists so the benchmark can show what the priority split buys.

Each message carries the W3C traceparent of the request that created it, so
a book's trace continues into every worker that prices part of it.
"""
import json
import os
import time
import uuid

import redis

from . import tracing

INTRADAY, BATCH, SHARED = "pricing:intraday", "pricing:batch", "pricing:tasks"
GROUP = "pricers"
JOB_TTL_S = 3600
STREAM_MAXLEN = 200_000
CLAIM_IDLE_MS = 10_000
MAX_DELIVERIES = 3


def mode() -> str:
    return os.getenv("QUEUE_MODE", "priority")


def streams() -> list[str]:
    """Every stream, in priority order (the API creates and reports all)."""
    return [SHARED] if mode() == "fifo" else [INTRADAY, BATCH]


def worker_streams() -> list[str]:
    """The streams this worker serves, in priority order. WORKER_STREAMS
    (e.g. "intraday" or "batch") splits workers into dedicated pools."""
    if mode() == "fifo":
        return [SHARED]
    wanted = os.getenv("WORKER_STREAMS", "intraday,batch").split(",")
    return [s for name, s in (("intraday", INTRADAY), ("batch", BATCH)) if name.strip() in wanted or s in wanted]


def stream_for(priority: str) -> str:
    if mode() == "fifo":
        return SHARED
    return INTRADAY if priority == "intraday" else BATCH


def _key(job_id: str, part: str) -> str:
    return f"job:{job_id}:{part}"


def ensure_groups(r: redis.Redis) -> None:
    for s in streams():
        try:
            r.xgroup_create(s, GROUP, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise


def submit(r: redis.Redis, kind: str, priority: str, tasks: list[dict], meta: dict,
           payload: bytes | None = None, request_id: str = "-") -> str:
    """Enqueues a job in one pipelined round trip, however many tasks it has."""
    job_id = uuid.uuid4().hex
    stream = stream_for(priority)
    pipe = r.pipeline(transaction=False)
    pipe.hset(_key(job_id, "meta"), mapping={
        "kind": kind, "n": len(tasks), "submitted_at": time.time(), "request_id": request_id,
        "meta": json.dumps(meta)})
    pipe.expire(_key(job_id, "meta"), JOB_TTL_S)
    if payload is not None:
        pipe.set(_key(job_id, "payload"), payload, ex=JOB_TTL_S)
    traceparent = tracing.inject()
    for i, task in enumerate(tasks):
        pipe.xadd(stream, {"job": job_id, "idx": i, "kind": kind, "task": json.dumps(task), "traceparent": traceparent},
                  maxlen=STREAM_MAXLEN, approximate=True)
    pipe.execute()
    return job_id


def payload(r: redis.Redis, job_id: str) -> bytes | None:
    return r.get(_key(job_id, "payload"))


def complete(r: redis.Redis, job_id: str, idx: int, result: dict, blob: bytes | None = None) -> None:
    pipe = r.pipeline(transaction=False)
    pipe.hset(_key(job_id, "results"), idx, json.dumps(result))
    pipe.expire(_key(job_id, "results"), JOB_TTL_S)
    if blob is not None:
        pipe.hset(_key(job_id, "blobs"), idx, blob)
        pipe.expire(_key(job_id, "blobs"), JOB_TTL_S)
    pipe.execute()


def meta(r: redis.Redis, job_id: str) -> dict | None:
    m = r.hgetall(_key(job_id, "meta"))
    if not m:
        return None
    return {"kind": m[b"kind"].decode(), "n": int(m[b"n"]), "submitted_at": float(m[b"submitted_at"]),
            "request_id": m[b"request_id"].decode(), "meta": json.loads(m[b"meta"])}


def done(r: redis.Redis, job_id: str) -> int:
    return r.hlen(_key(job_id, "results"))


def results(r: redis.Redis, job_id: str) -> dict[int, dict]:
    return {int(k): json.loads(v) for k, v in r.hgetall(_key(job_id, "results")).items()}


def blobs(r: redis.Redis, job_id: str) -> dict[int, bytes]:
    return {int(k): v for k, v in r.hgetall(_key(job_id, "blobs")).items()}


def wait(r: redis.Redis, job_id: str, n: int, timeout_s: float, poll_s: float = 0.02) -> bool:
    """Polls until all n results are in. A Redis error (a restart) is waited
    out rather than failing the request: with AOF persistence the job and its
    queued tasks survive the restart, and the workers carry on.
    shortcut: polls every 20 ms; switch to a blocking completion signal if
    concurrent books reach the hundreds (docs/CODE_AUDIT.md, finding 7)."""
    deadline = time.monotonic() + timeout_s
    with tracing.quiet():
        while time.monotonic() < deadline:
            try:
                if done(r, job_id) >= n:
                    return True
            except redis.RedisError:
                time.sleep(0.5)
            time.sleep(poll_s)
    return False


def _parse(resp) -> list[tuple[str, str, dict]]:
    return [(s.decode() if isinstance(s, bytes) else s, mid.decode(), fields)
            for s, msgs in resp or [] for mid, fields in msgs if fields]


def read(r: redis.Redis, consumer: str, block_ms: int = 1000) -> list[tuple[str, str, dict]]:
    """Next task, highest-priority stream first.

    One non-blocking read per stream in priority order; only when all are
    empty does the worker block, on all of them at once.
    """
    ss = worker_streams()
    for s in ss:
        msgs = _parse(r.xreadgroup(GROUP, consumer, {s: ">"}, count=1))
        if msgs:
            return msgs
    msgs = _parse(r.xreadgroup(GROUP, consumer, {s: ">" for s in ss}, count=1, block=block_ms))
    return sorted(msgs, key=lambda m: ss.index(m[0]))


def reclaim(r: redis.Redis, consumer: str) -> tuple[list, list]:
    """Takes over tasks idle for CLAIM_IDLE_MS (their worker likely died).

    Returns (retry, poison). A task delivered more than MAX_DELIVERIES times
    is poison: failing it beats letting one bad trade crash every worker in
    turn.
    """
    retry, poison = [], []
    for s in worker_streams():
        _, claimed, *_ = r.xautoclaim(s, GROUP, consumer, min_idle_time=CLAIM_IDLE_MS, start_id="0-0", count=10)
        claimed = [(mid.decode(), f) for mid, f in claimed if f]
        if not claimed:
            continue
        pending = r.xpending_range(s, GROUP, min=claimed[0][0], max=claimed[-1][0],
                                   count=len(claimed), consumername=consumer)
        deliveries = {p["message_id"].decode(): p["times_delivered"] for p in pending}
        for mid, f in claimed:
            (poison if deliveries.get(mid, 1) > MAX_DELIVERIES else retry).append((s, mid, f))
    return retry, poison


def ack(r: redis.Redis, stream: str, message_id: str) -> None:
    r.xack(stream, GROUP, message_id)


def backlog(r: redis.Redis) -> dict[str, dict]:
    """Per stream: tasks not yet delivered (lag) and delivered but unacked (pending)."""
    out = {}
    for s in streams():
        try:
            groups = r.xinfo_groups(s)
        except redis.ResponseError:
            groups = []
        g = next((g for g in groups if g["name"] in (GROUP, GROUP.encode())), None)
        out[s] = {"lag": (g.get("lag") or 0) if g else 0, "pending": g["pending"] if g else 0}
    return out
