import logging
import math
import os
import socket
import threading
import time
from collections import Counter as Tally
from contextlib import contextmanager, nullcontext

import numpy as np
import pricer_core
from prometheus_client import Counter, Gauge, Histogram
from pydantic import ValidationError

from . import tracing
from .cache import PriceCache, request_hash, seed_from_hash
from .models import European, PriceRequest, Simulation, Trade
from .store import STORE_ERRORS, Store

log = logging.getLogger(__name__)

PRICER_THREADS = int(os.getenv("PRICER_THREADS", "2"))
LOCK_WAIT_S = float(os.getenv("CACHE_LOCK_WAIT_S", "5"))
HOSTNAME = socket.gethostname()

CACHE = Counter("price_cache_total", "Cache lookups", ["result"])
COMPUTE = Histogram("price_compute_seconds", "Time in the C++ pricer", ["product"],
                    buckets=(.0001, .001, .005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10))
IN_FLIGHT = Gauge("compute_in_flight", "Pricings running in this process")
REJECTED = Counter("compute_rejected_total", "Requests rejected by admission control")


class Overloaded(Exception):
    pass


# Admission control. Each pricing runs PRICER_THREADS C++ threads, so
# COMPUTE_SLOTS x PRICER_THREADS is sized to the pod's CPU limit. Past that,
# more concurrent requests only add context switches and memory, so they queue
# here, and after ADMISSION_TIMEOUT_S get a 503 to retry on another pod.
_slots = threading.BoundedSemaphore(int(os.getenv("COMPUTE_SLOTS", "4")))
ADMISSION_TIMEOUT_S = float(os.getenv("ADMISSION_TIMEOUT_S", "10"))


@contextmanager
def admission():
    if not _slots.acquire(timeout=ADMISSION_TIMEOUT_S):
        REJECTED.inc()
        raise Overloaded("all compute slots busy")
    IN_FLIGHT.inc()
    try:
        yield
    finally:
        IN_FLIGHT.dec()
        _slots.release()


def _finite(x: float) -> float | None:
    return x if math.isfinite(x) else None


def _mc(p, sim: Simulation, seed: int, spot: float | None = None, vol: float | None = None):
    spot, vol = spot or p.spot, vol or p.vol
    args = dict(is_call=p.is_call, div_yield=p.div_yield, n_paths=sim.n_paths,
                antithetic=sim.antithetic, seed=seed, threads=PRICER_THREADS)
    if p.type == "european":
        return pricer_core.price_european_mc(spot, p.strike, p.rate, vol, p.maturity, **args)
    return pricer_core.price_asian_mc(spot, p.strike, p.rate, vol, p.maturity, n_fixings=p.n_fixings,
                                      control_variate=p.control_variate, **args)


def compute(req: PriceRequest, h: str, span: bool = True) -> dict:
    """Prices one request. span=False inside a book task, which records one
    span for all its Monte Carlo trades instead of one per trade."""
    p, sim = req.product, req.simulation
    t0 = time.perf_counter()
    attrs = {"product": p.type, "n_paths": sim.n_paths if p.steps else 0}
    with admission(), (tracing.tracer.start_as_current_span("pricer.compute", attributes=attrs) if span else nullcontext()):
        if p.type == "european" and p.method == "analytic":
            r = pricer_core.black_scholes(p.spot, p.strike, p.rate, p.div_yield, p.vol, p.maturity, p.is_call)
            out = {"price": r.price, "std_err": 0.0, "delta": r.delta, "vega": r.vega, "gamma": r.gamma,
                   "n_paths": 0, "method": "analytic", "reference_price": r.price}
        else:
            seed = seed_from_hash(h)
            r = _mc(p, sim, seed)
            out = {"price": r.price.value, "std_err": r.price.std_err, "delta": None, "vega": None,
                   "gamma": None, "n_paths": r.n_paths, "method": "mc", "reference_price": None,
                   "variance_reduction": _finite(r.variance_reduction) if p.type == "asian" else None}
            if p.type == "european":
                out["delta"], out["vega"] = r.delta.value, r.vega.value
                out["reference_price"] = pricer_core.black_scholes(
                    p.spot, p.strike, p.rate, p.div_yield, p.vol, p.maturity, p.is_call).price
            elif sim.greeks:
                # Central differences with the same seed: common random
                # numbers cancel most of the noise in the difference.
                ds, dv = 0.01 * p.spot, 0.01
                out["delta"] = (_mc(p, sim, seed, spot=p.spot + ds).price.value
                                - _mc(p, sim, seed, spot=p.spot - ds).price.value) / (2 * ds)
                out["vega"] = (_mc(p, sim, seed, vol=p.vol + dv).price.value
                               - _mc(p, sim, seed, vol=p.vol - dv).price.value) / (2 * dv)
    COMPUTE.labels(p.type).observe(time.perf_counter() - t0)
    return out


def _persist(store: Store, rows: list[dict]) -> None:
    try:
        store.write_prices(rows)
    except Exception as e:
        # The prices are computed and cached. A failed write to the price log
        # is logged and counted, not surfaced to the client.
        log.error("store write failed", extra={"rows": len(rows), "error": str(e)})
        STORE_ERRORS.inc()


def _log_row(instrument_id: str, req: PriceRequest, h: str, result: dict, job_id: str | None) -> dict:
    return {"instrument_id": instrument_id, "request_hash": h, "market_data_version": req.market_data_version,
            "job_id": job_id, **{k: result.get(k) for k in ("price", "std_err", "delta", "vega", "n_paths", "method")}}


def price_with_cache(req: PriceRequest, cache: PriceCache, store: Store) -> tuple[dict, bool, str]:
    """Single price: cache lookup, stampede-protected compute, cache fill,
    price log write. Returns (result, cached, request_hash)."""
    h = request_hash(req)
    hit = cache.get(h)
    if hit is not None:
        CACHE.labels("hit").inc()
        return hit, True, h
    CACHE.labels("miss" if cache.enabled else "disabled").inc()

    token = cache.acquire_lock(h)
    if token is None:
        # Another worker holds the lock and is computing this exact request.
        hit = cache.wait_for(h, LOCK_WAIT_S)
        if hit is not None:
            CACHE.labels("hit_after_wait").inc()
            return hit, True, h
        # Lock holder died or is slow; results are deterministic, so it is
        # safe to compute the same thing ourselves.
    try:
        result = compute(req, h)
        cache.set(h, result)
    finally:
        if token:
            cache.release_lock(h, token)
    _persist(store, [_log_row(req.instrument_id, req, h, result, None)])
    return result, False, h


def price_task(task: dict, cache: PriceCache, store: Store, job_id: str | None = None) -> dict:
    """Prices one task's slice of a book and pre-aggregates it, so the API
    gathers one small summary per task rather than every trade's result.

    Cache lookups and writes are one MGET and one pipelined SET for the whole
    slice. Analytic trades go through one vectorised C++ call.
    """
    md, sim = task["md"], Simulation(**task["sim"])
    rows: list[dict] = []
    reqs: list[tuple[Trade, PriceRequest, str] | None] = []
    for raw in task["trades"]:
        try:  # stream messages are a trust boundary too: validate again
            t = Trade.model_validate(raw)
            req = PriceRequest(instrument_id=t.trade_id, market_data_version=md, product=t.product, simulation=sim)
            reqs.append((t, req, request_hash(req)))
        except ValidationError as e:
            reqs.append(None)
            rows.append({"trade_id": str(raw.get("trade_id", "?")), "underlying": "", "quantity": 0.0,
                         "error": e.errors()[0]["msg"]})

    valid = [r for r in reqs if r is not None]
    results: dict[str, dict] = {}
    cached = set()
    for (_, _, h), hit in zip(valid, cache.get_many([h for _, _, h in valid])):
        if hit is not None:
            results[h] = hit
            cached.add(h)
    CACHE.labels("hit").inc(len(cached))
    CACHE.labels("miss").inc(len({h for _, _, h in valid}) - len(cached))

    todo = {h: req for _, req, h in valid if h not in results}
    analytic = {h: req for h, req in todo.items()
                if isinstance(req.product, European) and req.product.method == "analytic"}
    if analytic:
        ps = [req.product for req in analytic.values()]
        col = lambda name: np.array([getattr(p, name) for p in ps], dtype=float)  # noqa: E731
        with admission(), tracing.tracer.start_as_current_span("pricer.bs_batch", attributes={"options": len(ps)}):
            out = pricer_core.bs_batch(col("spot"), col("strike"), ps[0].rate, col("div_yield"), col("vol"),
                                       col("maturity"), np.array([p.is_call for p in ps]), threads=PRICER_THREADS)
        rates_differ = any(p.rate != ps[0].rate for p in ps)
        for i, (h, req) in enumerate(analytic.items()):
            if rates_differ and req.product.rate != ps[0].rate:
                results[h] = compute(req, h)  # rare: batch call takes one flat rate
            else:
                results[h] = {"price": float(out["price"][i]), "std_err": 0.0, "delta": float(out["delta"][i]),
                              "vega": float(out["vega"][i]), "gamma": float(out["gamma"][i]), "n_paths": 0,
                              "method": "analytic"}
    errors: dict[str, str] = {}
    mc = {h: req for h, req in todo.items() if h not in analytic}
    with tracing.tracer.start_as_current_span("pricer.mc_batch", attributes={"trades": len(mc)}):
        for h, req in mc.items():
            try:
                results[h] = compute(req, h, span=False)
            except Exception as e:  # noqa: BLE001 - one bad trade must not fail the slice
                errors[h] = str(e)
    cache.set_many({h: results[h] for h in todo if h in results})
    _persist(store, [_log_row(t.trade_id, req, h, results[h], job_id)
                     for t, req, h in valid if h in results and h not in cached] if task.get("persist", True) else [])

    # Aggregate. Trades on the same contract share one estimate (same hash,
    # same seed), so their Monte Carlo errors are perfectly correlated: add
    # quantities per hash first, then add variances across distinct hashes.
    pv, delta, vega, qty_by_hash = 0.0, Tally(), Tally(), Tally()
    n_failed = len(rows)
    for t, _, h in valid:
        r = results.get(h)
        if r is None:
            n_failed += 1
            rows.append({"trade_id": t.trade_id, "underlying": t.underlying, "quantity": t.quantity,
                         "error": errors.get(h, "not priced")})
            continue
        pv += t.quantity * r["price"]
        qty_by_hash[h] += t.quantity
        if r.get("delta") is not None:
            delta[t.underlying] += t.quantity * r["delta"]
        if r.get("vega") is not None:
            vega[t.underlying] += t.quantity * r["vega"]
        if task.get("include_trades"):
            rows.append({"trade_id": t.trade_id, "underlying": t.underlying, "quantity": t.quantity,
                         "price": r["price"], "std_err": r["std_err"], "delta": r.get("delta"),
                         "vega": r.get("vega"), "cached": h in cached})
    groups = [[h[:16], q, results[h]["std_err"]] for h, q in qty_by_hash.items() if results[h]["std_err"]]
    return {"pv": pv, "mc_groups": groups, "delta": dict(delta), "vega": dict(vega),
            "n": len(task["trades"]), "n_failed": n_failed,
            "n_cached": sum(1 for _, _, h in valid if h in cached),
            "errors": [r["error"] for r in rows if r.get("error")][:5],
            "trades": rows if task.get("include_trades") else [r for r in rows if r.get("error")][:20]}
