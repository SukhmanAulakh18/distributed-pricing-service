"""Incident 4: Redis goes away while a book is in flight."""
import time

from service import taskqueue
from service.breaker import CircuitBreaker
from service.cache import PriceCache
from tests.siege import harness as h
from tests.siege.harness import Flaky, Watch
from tests.test_portfolio import book
from tools import triage

BODY = book(12, include_trades=True, task_size=2)  # 6 tasks
KEY = {"Idempotency-Key": "siege-outage"}
CIRCUIT = ("dependency_circuit_state", {"dependency": "redis"})


def slow(w, stream, mid, fields):
    time.sleep(0.12)  # keeps tasks in flight when the outage starts


def test_redis_outage_mid_book(siege, r, make_client):
    baseline = siege.clean_book(BODY)
    taskqueue.ensure_groups(r)
    flaky = Flaky(r)
    pool = siege.pool(flaky)
    with make_client(cache=PriceCache(flaky), queue=flaky) as c:
        job = c.post("/portfolio/price", json=BODY, headers={**KEY, "Prefer": "respond-async"}).json()["job_id"]
        watch = Watch(("worker_tasks_total", {"kind": "portfolio", "outcome": "ok"}))
        pool.start("w1", hook=slow)
        pool.start("w2", hook=slow)
        assert h.wait_for(lambda: taskqueue.done(r, job) >= 1)
        fault = siege.fault("Redis unreachable for 0.4 s with the book half done",
                            "API answers 503 + Retry-After, breaker opens then closes, retry with the same "
                            "key completes the same job once", "RedisCircuitOpen")
        left = 6 - taskqueue.done(r, job)
        flaky.down.set()
        poll, retry = c.get(f"/jobs/{job}"), c.post("/portfolio/price", json=BODY, headers=KEY)
        breaker = CircuitBreaker("redis", threshold=3, cooldown_s=0.3)
        probe = PriceCache(flaky, breaker=breaker)
        for _ in range(3):
            probe.get_many(["k"])
        opened, gauge = breaker.state, h.sample(*CIRCUIT[:1], **CIRCUIT[1])
        # What the triage tool says about the same /metrics sample.
        finding = triage.analyze({"health": {}, "metrics": {'dependency_circuit_state{dependency="redis"}': gauge}})
        time.sleep(0.4)
        flaky.down.clear()
        time.sleep(0.3)  # past the breaker cooldown
        probe.get_many(["k"])
        closed = breaker.state
        again = c.post("/portfolio/price", json={**BODY, "timeout_s": 20}, headers=KEY)
        got = siege.readback_book(c, job)
    watch.specs.append(CIRCUIT)
    watch.before.append(0.0)  # the gauge is 0 when healthy
    diffs = h.diff(baseline, got)
    fault.finish(watch, f"{left} tasks left at outage; during: {poll.status_code}/{retry.status_code} "
                 f"Retry-After={retry.headers.get('retry-after')}, breaker {opened} then {closed}; "
                 f"{len(diffs)} diffs, {r.xlen(taskqueue.INTRADAY)} messages",
                 outage_hit_a_running_book=left > 0,
                 api_503_with_retry_after=poll.status_code == retry.status_code == 503
                 and "retry-after" in retry.headers,
                 breaker_opened_and_closed=opened == "open" and closed == "closed",
                 circuit_gauge_read_open=gauge == 2,
                 triage_routes_to_platform=any(f.owner == triage.OWNERS["platform"] for f in finding),
                 retry_same_job_complete=again.status_code == 200 and again.json()["job_id"] == job,
                 identical_to_baseline=got is not None and not diffs,
                 enqueued_once=r.xlen(taskqueue.INTRADAY) == baseline["tasks"],
                 tasks_completed_metric_moved=bool(watch.moved()))
