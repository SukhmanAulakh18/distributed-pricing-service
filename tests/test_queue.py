import threading
import time

from service import taskqueue, worker
from service.cache import PriceCache
from service.pricing import price_task
from service.store import NullStore
from service.worker import Worker
from tests.test_portfolio import trade


def test_error_never_overwrites_a_good_result(r):
    taskqueue.complete(r, "j", 0, {"pv": 1.0})
    taskqueue.complete(r, "j", 0, {"error": "duplicate run failed"})
    assert taskqueue.results(r, "j")[0] == {"pv": 1.0}


def test_success_replaces_an_error(r):
    taskqueue.complete(r, "j", 0, {"error": "exceeded max deliveries"})
    taskqueue.complete(r, "j", 0, {"pv": 2.0})
    assert taskqueue.results(r, "j")[0] == {"pv": 2.0}


def test_fail_poison_keeps_existing_result(r):
    taskqueue.ensure_groups(r)
    job = taskqueue.submit(r, "portfolio", "intraday", [{"md": "md", "sim": {}, "trades": []}], {})
    (s, mid, fields), = taskqueue.read(r, "w", block_ms=10)
    taskqueue.complete(r, job, 0, {"pv": 3.0})
    Worker(r, PriceCache(r), NullStore(), consumer="w").fail_poison(s, mid, fields)
    assert taskqueue.results(r, job)[0] == {"pv": 3.0}


def test_long_running_task_is_not_reclaimed(r, monkeypatch):
    monkeypatch.setattr(taskqueue, "CLAIM_IDLE_MS", 2000)
    taskqueue.ensure_groups(r)
    job = taskqueue.submit(r, "portfolio", "intraday",
                           [{"md": "md", "sim": {"n_paths": 5_000}, "trades": [trade(0)]}], {})

    def slow(*a, **k):
        time.sleep(5)  # 2.5x CLAIM_IDLE_MS; heartbeats every ~0.67 s
        return price_task(*a, **k)
    monkeypatch.setattr(worker, "price_task", slow)
    t = threading.Thread(target=lambda: Worker(r, PriceCache(r), NullStore(), "slow").run_once(block_ms=10))
    t.start()
    stolen = []
    while t.is_alive():
        time.sleep(0.25)
        stolen += taskqueue.reclaim(r, "other")[0] + taskqueue.reclaim(r, "other")[1]
    t.join()
    assert stolen == [] and taskqueue.results(r, job)[0]["n_failed"] == 0
    assert all(b["pending"] == 0 for b in taskqueue.backlog(r).values())


def test_vol_below_bump_size_still_prices_greeks(make_client):
    p = {"type": "asian", "spot": 100, "strike": 100, "rate": 0.03, "vol": 0.005, "maturity": 1.0, "n_fixings": 12}
    with make_client() as c:
        resp = c.post("/price", json={"instrument_id": "x", "market_data_version": "md", "product": p,
                                      "simulation": {"n_paths": 20_000, "greeks": True}})
    assert resp.status_code == 200 and resp.json()["vega"] is not None


def test_heartbeat_does_not_steal_back_a_reclaimed_task(r, monkeypatch):
    monkeypatch.setattr(taskqueue, "CLAIM_IDLE_MS", 0)
    taskqueue.ensure_groups(r)
    taskqueue.submit(r, "portfolio", "intraday", [{"n": 1}], {})
    (s, mid, _), = taskqueue.read(r, "A", block_ms=10)
    assert taskqueue.heartbeat(r, s, "A", mid)
    taskqueue.reclaim(r, "B")
    assert taskqueue.heartbeat(r, s, "A", mid) is False
    (p,) = r.xpending_range(s, taskqueue.GROUP, min=mid, max=mid, count=1)
    assert p["consumer"] in ("B", b"B")


def test_resubmitted_key_after_meta_expiry_starts_fresh(r):
    taskqueue.ensure_groups(r)
    taskqueue.submit(r, "portfolio", "intraday", [{"n": 1}], {}, job_id="j")
    taskqueue.complete(r, "j", 0, {"error": "stale"})
    r.set("job:j:recorded", 1)
    r.delete("job:j:meta")  # meta expired; results (refreshed TTL) did not
    taskqueue.submit(r, "portfolio", "intraday", [{"n": 1}], {}, job_id="j", payload=b"p")
    assert taskqueue.done(r, "j") == 0 and not r.exists("job:j:recorded")
    assert r.xlen(taskqueue.INTRADAY) == 2 and taskqueue.payload(r, "j") == b"p"
