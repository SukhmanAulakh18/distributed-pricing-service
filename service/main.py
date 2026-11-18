import hashlib
import json
import logging
import math
import os
import time
import uuid
from collections import Counter as Tally
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from . import exposure, logging_setup, taskqueue, tracing
from .cache import PriceCache
from .models import (ExposureJob, ExposureRequest, ExposureResult, PortfolioRequest,
                     PortfolioResponse, PriceRequest, PriceResponse)
from .pricing import HOSTNAME, Overloaded, price_task, price_with_cache
from .store import Store, build_store

logging_setup.configure()
log = logging.getLogger("pricer.api")

REQUESTS = Counter("price_requests_total", "Pricing requests", ["outcome"])
HTTP = Histogram("http_request_seconds", "HTTP latency by route", ["method", "route", "status"],
                 buckets=(.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30, 60))
LATENCY = Histogram("price_latency_seconds", "End-to-end /price latency",
                    buckets=(.0005, .001, .0025, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10))
JOBS = Counter("jobs_total", "Distributed jobs", ["kind", "mode", "outcome"])

LOCAL_POOL = ThreadPoolExecutor(max_workers=int(os.getenv("LOCAL_POOL_SIZE", "4")))


def build_cache() -> PriceCache:
    url = os.getenv("REDIS_URL")
    if not url or os.getenv("CACHE_ENABLED", "true").lower() == "false":
        return PriceCache(None)
    client = redis.Redis.from_url(url, socket_timeout=0.2, socket_connect_timeout=0.2)
    return PriceCache(client, ttl_seconds=int(os.getenv("CACHE_TTL_S", "30")))


def build_queue() -> redis.Redis | None:
    """Separate client for the queue: longer timeouts than the cache, since
    queue calls carry whole books and sit off the single-price hot path."""
    url = os.getenv("REDIS_URL")
    if not url or os.getenv("PORTFOLIO_MODE", "distributed") == "local":
        return None
    return redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cache = getattr(app.state, "cache", None) or build_cache()
    app.state.store = getattr(app.state, "store", None) or build_store()
    if not hasattr(app.state, "queue"):
        app.state.queue = build_queue()
    app.state.local_results = {}
    if app.state.queue is not None:
        try:
            taskqueue.ensure_groups(app.state.queue)
        except redis.RedisError as e:
            log.warning("could not create consumer groups yet", extra={"error": str(e)})
    yield
    if hasattr(app.state.store, "flush"):
        app.state.store.flush()


app = FastAPI(title="Distributed Pricing Service", lifespan=lifespan)
tracing.setup("pricing-api", app)


@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    token = logging_setup.request_id.set(rid)
    t0 = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["x-request-id"] = rid
        response.headers["x-served-by"] = HOSTNAME
        if (tid := tracing.trace_id()) != "-":
            response.headers["x-trace-id"] = tid  # open it in Jaeger
        return response
    finally:
        elapsed = time.perf_counter() - t0
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        HTTP.labels(request.method, path, str(status)).observe(elapsed)
        if path not in ("/metrics", "/health/live", "/health/ready"):
            log.info("request", extra={"method": request.method, "path": path, "status": status,
                                       "ms": round(elapsed * 1e3, 2)})
        logging_setup.request_id.reset(token)


@app.exception_handler(Overloaded)
async def overloaded(_: Request, exc: Overloaded):
    return JSONResponse({"detail": str(exc)}, status_code=503, headers={"Retry-After": "1"})


def unavailable(dependency: str, e: Exception) -> HTTPException:
    """A named 503 instead of a bare 500, so callers and triage can tell a
    dependency outage from a bug in this service."""
    log.warning("dependency unavailable", extra={"dependency": dependency, "error": str(e)})
    return HTTPException(503, f"{dependency} unavailable", headers={"Retry-After": "5"})


# Plain `def` endpoints run in FastAPI's threadpool. The C++ calls release
# the GIL, so concurrent requests really do price in parallel.
@app.post("/price", response_model=PriceResponse)
def price(req: PriceRequest) -> PriceResponse:
    t0 = time.perf_counter()
    result, cached, h = price_with_cache(req, app.state.cache, app.state.store)
    REQUESTS.labels("hit" if cached else "computed").inc()
    LATENCY.observe(time.perf_counter() - t0)
    return PriceResponse(**result, cached=cached, market_data_version=req.market_data_version, request_hash=h)


def _assemble(job_id: str, mode: str, spec: dict, results: dict[int, dict], n_tasks: int,
              elapsed_ms: float) -> PortfolioResponse:
    """spec is what the job meta stores about the book (book_id, n_trades,
    task_size, include_trades), so a poll can rebuild the sync response."""
    pv, delta, vega, workers, qty, se = 0.0, Tally(), Tally(), Tally(), Tally(), {}
    n_failed = n_cached = 0
    rows: list[dict] = []
    for i in range(n_tasks):
        size = min(spec["task_size"], spec["n_trades"] - i * spec["task_size"])
        r = results.get(i)
        if r is None or "pv" not in r:
            n_failed += size
            rows.append({"trade_id": f"task-{i}", "underlying": "", "quantity": 0.0,
                         "error": (r or {}).get("error", "not completed before timeout")})
            continue
        pv += r["pv"]
        for h, q, s in r["mc_groups"]:
            qty[h] += q
            se[h] = s
        delta.update(r["delta"])
        vega.update(r["vega"])
        n_failed += r["n_failed"]
        n_cached += r["n_cached"]
        workers[r.get("priced_by", HOSTNAME)] += 1
        rows.extend(r["trades"])
    return PortfolioResponse(
        job_id=job_id, book_id=spec["book_id"], mode=mode, n_trades=spec["n_trades"], n_failed=n_failed,
        n_cached=n_cached, pv=pv, pv_std_err=math.sqrt(sum((qty[h] * se[h]) ** 2 for h in qty)),
        delta_by_underlying=dict(delta), vega_by_underlying=dict(vega), tasks=n_tasks,
        workers_used=dict(workers), elapsed_ms=round(elapsed_ms, 2),
        trades=rows if spec["include_trades"] or rows else None)


def _record(job_id: str, kind: str, submitted_at: float, status: str, n_tasks: int, n_failed: int,
            elapsed_ms: float, workers: int, detail: dict) -> None:
    try:
        app.state.store.record_job({"job_id": job_id, "kind": kind, "submitted_at": submitted_at, "status": status,
                                    "n_tasks": n_tasks, "n_failed": n_failed, "elapsed_ms": elapsed_ms,
                                    "workers": workers, "detail": detail})
    except Exception as e:
        log.warning("could not record job", extra={"job_id": job_id, "error": str(e)})


def _fingerprint(body: PortfolioRequest) -> str:
    """Same book, same fingerprint; timeout_s is how long to wait, not what to price."""
    canon = json.dumps(body.model_dump(mode="json", exclude={"timeout_s"}), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def _distributed_result(q: redis.Redis, job_id: str, m: dict, finished: bool,
                        wall_ms: float) -> tuple[PortfolioResponse, str]:
    """Assembles a queued book from stored state and, once the job is over,
    records it exactly once however many callers (retries, polls) get here."""
    results = taskqueue.results(q, job_id)
    # Once complete, elapsed comes from stored timestamps so every read of the job agrees.
    stamps = [r["finished_at"] for r in results.values() if "finished_at" in r]
    elapsed_ms = (max(stamps) - m["submitted_at"]) * 1e3 if finished and stamps else wall_ms
    out = _assemble(job_id, "distributed", m["meta"], results, m["n"], elapsed_ms)
    status = "complete" if out.n_failed == 0 else ("partial" if finished else "running")
    if finished and q.set(f"job:{job_id}:recorded", 1, nx=True, ex=taskqueue.JOB_TTL_S):
        JOBS.labels("portfolio", "distributed", status).inc()
        _record(job_id, "portfolio", m["submitted_at"], status, m["n"], out.n_failed, out.elapsed_ms,
                len(out.workers_used), {"book_id": out.book_id, "n_trades": out.n_trades})
    return out, status


@app.post("/portfolio/price", response_model=PortfolioResponse)
def price_portfolio(body: PortfolioRequest, response: Response,
                    idempotency_key: str | None = Header(None, min_length=1, max_length=128),
                    prefer: str | None = Header(None)) -> PortfolioResponse:
    """Prices a book on the worker pool (intraday priority) and aggregates PV,
    PV standard error, and delta and vega by underlying.

    Idempotency-Key makes a retry safe: the key fixes the job id, so a client
    that lost its connection re-sends and waits on the same job instead of
    pricing the book twice. `Prefer: respond-async` returns 202 at once (ignored without a queue:
    local mode prices inline and answers 200)."""
    t0, submitted_at = time.perf_counter(), time.time()
    ts = body.task_size
    tasks = [{"trades": [t.model_dump() for t in body.trades[i:i + ts]], "md": body.market_data_version,
              "sim": body.simulation.model_dump(), "include_trades": body.include_trades, "persist": body.persist}
             for i in range(0, len(body.trades), ts)]
    q: redis.Redis | None = app.state.queue
    job_id = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32] if idempotency_key else None
    spec = {"book_id": body.book_id, "n_trades": len(body.trades), "task_size": ts,
            "include_trades": body.include_trades, "fp": _fingerprint(body)}
    if q is not None:
        try:
            with tracing.tracer.start_as_current_span("book.submit", attributes={"tasks": len(tasks),
                                                                                 "trades": len(body.trades)}):
                job_id = taskqueue.submit(q, "portfolio", "intraday", tasks, spec,
                                          request_id=logging_setup.request_id.get(), job_id=job_id)
            m = taskqueue.meta(q, job_id)
        except redis.RedisError as e:
            if idempotency_key:  # a local fallback could price a book a retry already queued
                raise unavailable("queue (Redis)", e) from e
            log.warning("queue submit failed, pricing locally", extra={"error": str(e)})
            q = None
    if q is not None:
        if m is None or m["meta"].get("fp") != spec["fp"]:
            raise HTTPException(409, "Idempotency-Key was already used with a different request body")
        if prefer and "respond-async" in prefer.lower():
            return JSONResponse({"job_id": job_id, "status": "running", "status_url": f"/jobs/{job_id}"},
                                status_code=202, headers={"Location": f"/jobs/{job_id}",
                                                          "Preference-Applied": "respond-async"})
        with tracing.tracer.start_as_current_span("book.wait", attributes={"job.id": job_id}):
            finished = taskqueue.wait(q, job_id, m["n"], body.timeout_s)
        try:
            out, status = _distributed_result(q, job_id, m, finished, (time.perf_counter() - t0) * 1e3)
        except redis.RedisError as e:
            raise unavailable("queue (Redis)", e) from e
        if not finished:
            response.status_code = 206
    else:
        job_id = uuid.uuid4().hex
        results = dict(enumerate(LOCAL_POOL.map(
            lambda t: {**price_task(t, app.state.cache, app.state.store), "priced_by": HOSTNAME}, tasks)))
        out = _assemble(job_id, "local", spec, results, len(tasks), (time.perf_counter() - t0) * 1e3)
        status = "complete" if out.n_failed == 0 else "partial"
        JOBS.labels("portfolio", "local", status).inc()
        _record(job_id, "portfolio", submitted_at, status, len(tasks), out.n_failed, out.elapsed_ms,
                len(out.workers_used), {"book_id": body.book_id, "n_trades": out.n_trades})
    log.info("portfolio done", extra={"job_id": job_id, "mode": out.mode, "n": out.n_trades, "failed": out.n_failed,
                                      "tasks": len(tasks), "workers": len(out.workers_used), "ms": out.elapsed_ms})
    return out


def exposure_meta(body: ExposureRequest, payload: dict) -> dict:
    """What the result needs besides the blocks: stored with the job, so any
    API pod can assemble it."""
    return {"netting_set_id": body.netting_set_id, "md": body.market_data_version, "grid": body.grid,
            "sim_grid": payload["grid"], "report": payload["report"],
            "tail_k": body.tail_k, "rate": body.rate, "recovery": body.recovery,
            "spread_bps": body.cds_spread_bps,
            "own": None if body.own_credit is None else [body.own_credit.recovery, body.own_credit.cds_spread_bps],
            "csa": None if body.csa is None else body.csa.model_dump()}


@app.post("/risk/exposure", response_model=ExposureJob, status_code=202)
def submit_exposure(body: ExposureRequest) -> ExposureJob:
    """Queues an exposure run on the batch stream and returns at once; poll
    the status URL. Batch work never delays intraday tasks (see taskqueue)."""
    payload, tasks = exposure.plan(body)
    meta = exposure_meta(body, payload)
    q: redis.Redis | None = app.state.queue
    if q is not None:
        try:
            job_id = taskqueue.submit(q, "exposure", "batch", tasks, meta, payload=json.dumps(payload).encode(),
                                      request_id=logging_setup.request_id.get())
        except redis.RedisError as e:
            # Batch work waits for the queue rather than running a
            # minutes-long job inline on an API pod.
            raise unavailable("batch queue (Redis)", e) from e
    else:  # no Redis: run inline so the endpoint still works in local dev
        job_id, t0 = uuid.uuid4().hex, time.time()
        blocks = [exposure.run_block(payload, t["start"], t["count"], 2) for t in tasks]
        result = _exposure_result(job_id, meta, blocks, {HOSTNAME: len(tasks)}, (time.time() - t0) * 1e3)
        _persist_exposure(result, meta, t0)
        app.state.local_results[job_id] = result
    return ExposureJob(job_id=job_id, n_blocks=len(tasks), status_url=f"/risk/exposure/{job_id}")


def _exposure_today(v0: float, csa: dict | None) -> tuple[float, float]:
    """(EE, ENE) at t = 0, where there is no uncertainty: the collateral held
    today is the CSA's call on today's value (see core: csa_target)."""
    if csa is None:
        return max(v0, 0.0), max(-v0, 0.0)
    held = max(v0 - csa["threshold_recv"], 0.0) - max(-v0 - csa["threshold_post"], 0.0)
    return max(v0 - held - csa["independent_amount"], 0.0), max(held - v0, 0.0)


def _exposure_result(job_id: str, meta: dict, blocks: list, workers: dict, elapsed_ms: float) -> ExposureResult:
    grid = meta["grid"]
    # Jobs queued before CSA support carry no sim_grid: they simulated on grid.
    merged = exposure.merge(blocks, meta.get("sim_grid", grid), meta["tail_k"], meta.get("report"))
    v0, own = merged["v0"], meta.get("own")
    cpty = (meta["recovery"], meta["spread_bps"])

    def xva(sfx: str) -> dict:
        """CVA on EE and, with our own credit, DVA on ENE (the mirror image:
        we default first while owing them) and bilateral CVA = CVA - DVA."""
        ee, ene = merged["ee" + sfx], merged["ene" + sfx]
        ee0, ene0 = _exposure_today(v0, meta.get("csa") if sfx else None)
        out = {"cva": exposure.cva(grid, ee, meta["rate"], *cpty, ee0=ee0)}
        if own:
            bil_cva = exposure.cva(grid, ee, meta["rate"], *cpty, ee0=ee0, own=tuple(own))
            out["dva"] = exposure.cva(grid, ene, meta["rate"], *own, ee0=ene0, own=cpty)
            out["bilateral_cva"] = bil_cva - out["dva"]
        return out

    base = xva("")
    fields = dict(ee=merged["ee"], pfe=merged["pfe"], ee_se=merged["ee_se"], ene=merged["ene"],
                  epe=exposure.epe(grid, merged["ee"], max(v0, 0.0)), peak_pfe=max(merged["pfe"]), v0=v0, **base)
    if "ee_csa" in merged:
        csa = xva("_csa")
        fields.update(ee_csa=merged["ee_csa"], pfe_csa=merged["pfe_csa"], epe_csa=exposure.epe(grid, merged["ee_csa"], _exposure_today(v0, meta.get("csa"))[0]),
                      peak_pfe_csa=max(merged["pfe_csa"]), cva_csa=csa["cva"], dva_csa=csa.get("dva"),
                      bilateral_cva_csa=csa.get("bilateral_cva"))
    return ExposureResult(
        job_id=job_id, status="complete", done=len(blocks), n_blocks=len(blocks),
        netting_set_id=meta["netting_set_id"], grid=grid, n_scenarios=merged["n_scenarios"],
        workers_used=workers, elapsed_ms=round(elapsed_ms, 2), **fields)


def _persist_exposure(result: ExposureResult, meta: dict, submitted_at: float) -> None:
    try:
        app.state.store.write_exposure({**result.model_dump(), "market_data_version": meta["md"]})
    except Exception as e:
        log.warning("could not store exposure run", extra={"job_id": result.job_id, "error": str(e)})
    _record(result.job_id, "exposure", submitted_at, "complete", result.n_blocks, 0, result.elapsed_ms,
            len(result.workers_used), {"netting_set_id": meta["netting_set_id"], "n_scenarios": result.n_scenarios})


@app.get("/risk/exposure/{job_id}", response_model=ExposureResult)
def exposure_status(job_id: str) -> ExposureResult:
    if job_id in app.state.local_results:
        return app.state.local_results[job_id]
    q: redis.Redis | None = app.state.queue
    if q is None:
        raise HTTPException(404, "job not found")
    try:
        return _exposure_status(q, job_id)
    except redis.RedisError as e:
        raise unavailable("job state (Redis)", e) from e


def _exposure_status(q: redis.Redis, job_id: str) -> ExposureResult:
    final = q.get(f"job:{job_id}:final")
    if final:
        return ExposureResult.model_validate_json(final)
    m = taskqueue.meta(q, job_id)
    if m is None or m["kind"] != "exposure":
        raise HTTPException(404, "job not found or expired")
    n, done = m["n"], taskqueue.done(q, job_id)
    if done < n:
        return ExposureResult(job_id=job_id, status="running", done=done, n_blocks=n)
    results = taskqueue.results(q, job_id)
    errors = [r["error"] for r in results.values() if "error" in r]
    if errors:
        if q.set(f"job:{job_id}:recorded", 1, nx=True, ex=taskqueue.JOB_TTL_S):
            JOBS.labels("exposure", "distributed", "failed").inc()
            _record(job_id, "exposure", m["submitted_at"], "failed", n, len(errors), 0.0, 0,
                    {"netting_set_id": m["meta"]["netting_set_id"], "errors": errors[:3]})
        return ExposureResult(job_id=job_id, status="failed", done=done, n_blocks=n, errors=errors[:5])
    # Merge on first read once every block is in. Blocks are deterministic,
    # so concurrent readers compute the same answer; SET NX keeps one copy and
    # only the first writer persists it.
    blobs = taskqueue.blobs(q, job_id)
    workers = Tally(r["priced_by"] for r in results.values())
    elapsed_ms = (max(r["finished_at"] for r in results.values()) - m["submitted_at"]) * 1e3
    result = _exposure_result(job_id, m["meta"], [(results[i], blobs[i]) for i in range(n)], dict(workers),
                              elapsed_ms)
    if q.set(f"job:{job_id}:final", result.model_dump_json(), nx=True, ex=taskqueue.JOB_TTL_S):
        JOBS.labels("exposure", "distributed", "complete").inc()
        _persist_exposure(result, m["meta"], m["submitted_at"])
    return result


@app.get("/risk/exposure/latest/{netting_set_id}")
def latest_exposure(netting_set_id: str):
    try:
        run = app.state.store.latest_exposure(netting_set_id)
    except Exception as e:
        raise unavailable("exposure history (Cassandra)", e) from e
    if run is None:
        raise HTTPException(404, "no stored exposure run for this netting set")
    return run


@app.get("/jobs")
def recent_jobs(limit: int = Query(50, ge=1, le=500)):
    """Today's jobs, newest first, from Cassandra. The triage tool's starting point."""
    try:
        return app.state.store.recent_jobs(limit)
    except Exception as e:
        raise unavailable("job history (Cassandra)", e) from e


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    q = app.state.queue
    if q is None:
        raise HTTPException(404, "distributed mode not enabled")
    try:
        m = taskqueue.meta(q, job_id)
        if m is None:
            raise HTTPException(404, "job not found or expired")
        done = taskqueue.done(q, job_id)
        info = {"job_id": job_id, **m, "done": done, "complete": done >= m["n"]}
        if m["kind"] != "portfolio":
            return info
        if done < m["n"]:
            return {**info, "status": "running"}
        if not {"task_size", "n_trades", "book_id"} <= m["meta"].keys():
            return info  # queued before the result spec was stored: no assembled body
        out, status = _distributed_result(q, job_id, m, True, 0.0)
    except redis.RedisError as e:
        raise unavailable("job state (Redis)", e) from e
    # "partial" (some trades failed) is reported as failed; the body says which.
    return {**info, "status": "complete" if status == "complete" else "failed", "result": out.model_dump()}


@app.get("/queue")
def queue_backlog():
    if app.state.queue is None:
        return {"mode": "local"}
    try:
        return {"mode": taskqueue.mode(), "streams": taskqueue.backlog(app.state.queue)}
    except redis.RedisError as e:
        raise unavailable("queue (Redis)", e) from e


@app.get("/prices/{instrument_id}")
def latest_prices(instrument_id: str, limit: int = Query(10, ge=1, le=100)):
    try:
        return app.state.store.latest_prices(instrument_id, limit)
    except Exception as e:
        raise unavailable("price log (Cassandra)", e) from e


# async: no blocking calls, so it never queues behind book waits in the threadpool.
@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    """Readiness reflects only this pod: startup finished, so it can price.

    Shared dependencies are reported but deliberately do not gate readiness.
    If Cassandra is down, every pod would fail the same check at once and the
    Service would have no endpoints, turning a degraded price log into a full
    outage. Pricing does not need Cassandra, and Redis is optional.
    """
    cache: PriceCache = app.state.cache
    store: Store = app.state.store
    checks = {"redis": "disabled" if not cache.enabled else ("ok" if cache.ping() else "down"),
              "cassandra": "ok" if store.ping() else "down"}
    degraded = "down" in checks.values()
    return {"ready": True, "status": "degraded" if degraded else "ok", "checks": checks}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
