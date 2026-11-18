import time

import pytest

from service import taskqueue
from service.cache import PriceCache
from tests.test_api import BrokenRedis
from tests.test_portfolio import book

KEY = {"Idempotency-Key": "client-retry-1"}


def queued(r):
    return sum(b["lag"] + b["pending"] for b in taskqueue.backlog(r).values())


def test_retry_with_same_key_returns_identical_result_and_enqueues_once(make_client, r, workers):
    workers()
    with make_client(cache=PriceCache(r), queue=r) as c:
        first = c.post("/portfolio/price", json={**book(10), "timeout_s": 20}, headers=KEY).json()
        total = r.xlen(taskqueue.INTRADAY)
        again = c.post("/portfolio/price", json={**book(10), "timeout_s": 5}, headers=KEY).json()
    assert first == again and first["n_failed"] == 0
    assert total == first["tasks"] == r.xlen(taskqueue.INTRADAY)


def test_retry_while_first_is_still_queued_enqueues_once(make_client, r):
    taskqueue.ensure_groups(r)  # no workers yet
    with make_client(cache=PriceCache(r), queue=r) as c:
        a = c.post("/portfolio/price", json={**book(6), "timeout_s": 0.1}, headers=KEY)
        b = c.post("/portfolio/price", json={**book(6), "timeout_s": 0.1}, headers=KEY)
    assert a.status_code == b.status_code == 206
    assert a.json()["job_id"] == b.json()["job_id"] and r.xlen(taskqueue.INTRADAY) == 2


def test_same_key_different_body_is_409_and_enqueues_nothing(make_client, r):
    taskqueue.ensure_groups(r)
    with make_client(cache=PriceCache(r), queue=r) as c:
        c.post("/portfolio/price", json={**book(6), "timeout_s": 0.1}, headers=KEY)
        n = r.xlen(taskqueue.INTRADAY)
        resp = c.post("/portfolio/price", json={**book(7), "timeout_s": 0.1}, headers=KEY)
    assert resp.status_code == 409 and r.xlen(taskqueue.INTRADAY) == n


def test_key_is_length_limited(make_client, r):
    with make_client(cache=PriceCache(r), queue=r) as c:
        assert c.post("/portfolio/price", json=book(2), headers={"Idempotency-Key": "k" * 129}).status_code == 422


def test_redis_error_with_key_is_503_not_local_fallback(make_client):
    with make_client(queue=BrokenRedis()) as c:
        resp = c.post("/portfolio/price", json=book(2), headers=KEY)
        assert resp.status_code == 503 and "retry-after" in resp.headers
        assert c.post("/portfolio/price", json=book(2)).json()["mode"] == "local"  # no key: as before


def test_async_then_poll(make_client, r, workers):
    taskqueue.ensure_groups(r)
    with make_client(cache=PriceCache(r), queue=r) as c:
        resp = c.post("/portfolio/price", json=book(6), headers={**KEY, "Prefer": "respond-async"})
        assert resp.status_code == 202
        loc = resp.headers["location"]
        assert loc == f"/jobs/{resp.json()['job_id']}"
        assert c.get(loc).json()["status"] == "running"
        workers()
        deadline = time.time() + 20
        while (poll := c.get(loc).json())["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
        sync = c.post("/portfolio/price", json=book(6), headers=KEY).json()
    assert poll["status"] == "complete" and poll["result"] == sync


def test_job_is_recorded_once(make_client, r, workers):
    class Rec:
        def __init__(self): self.jobs = []
        def record_job(self, row): self.jobs.append(row)
        def write_prices(self, rows): pass
    store = Rec()
    workers()
    with make_client(cache=PriceCache(r), queue=r, store=store) as c:
        for _ in range(3):
            c.post("/portfolio/price", json={**book(4), "timeout_s": 20}, headers=KEY)
        c.get(f"/jobs/{c.post('/portfolio/price', json=book(4), headers=KEY).json()['job_id']}")
    assert len(store.jobs) == 1


def test_no_key_still_prices_each_request_as_its_own_job(make_client, r, workers):
    workers()
    with make_client(cache=PriceCache(r), queue=r) as c:
        a = c.post("/portfolio/price", json={**book(4), "timeout_s": 20}).json()
        b = c.post("/portfolio/price", json={**book(4), "timeout_s": 20}).json()
    assert a["job_id"] != b["job_id"] and a["pv"] == pytest.approx(b["pv"], rel=1e-12)


def test_poll_of_pre_upgrade_job_does_not_500(make_client, r):
    taskqueue.ensure_groups(r)
    job = taskqueue.submit(r, "portfolio", "intraday", [{"n": 1}], {"book_id": "B1"})  # old meta shape
    taskqueue.complete(r, job, 0, {"pv": 1.0})
    with make_client(cache=PriceCache(r), queue=r) as c:
        resp = c.get(f"/jobs/{job}")
    assert resp.status_code == 200 and resp.json()["complete"] and "result" not in resp.json()


def test_poll_unknown_job_is_404(make_client, r):
    with make_client(cache=PriceCache(r), queue=r) as c:
        assert c.get("/jobs/nope").status_code == 404


def test_async_without_queue_prices_inline(make_client):
    with make_client() as c:
        resp = c.post("/portfolio/price", json=book(3), headers={**KEY, "Prefer": "respond-async"})
    assert resp.status_code == 200 and resp.json()["mode"] == "local"


def test_reused_key_after_meta_expiry_reprices(make_client, r, workers):
    workers()
    with make_client(cache=PriceCache(r), queue=r) as c:
        first = c.post("/portfolio/price", json={**book(4), "timeout_s": 20}, headers=KEY).json()
        r.delete(f"job:{first['job_id']}:meta")
        again = c.post("/portfolio/price", json={**book(4), "timeout_s": 20}, headers=KEY).json()
    assert again["n_failed"] == 0 and again["elapsed_ms"] > 0 and again["pv"] == first["pv"]
    assert r.xlen(taskqueue.INTRADAY) == 2 * first["tasks"]
