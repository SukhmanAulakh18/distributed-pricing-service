import math

import pytest

from service import taskqueue
from service.cache import PriceCache
from service.pricing import price_task
from service.store import NullStore
from service.worker import Worker

BASE = {"spot": 100, "rate": 0.03, "vol": 0.2, "maturity": 1.0}


def trade(i, **product):
    kinds = ["european", "asian"]
    p = {**BASE, "type": kinds[i % 2], "strike": 90 + 5 * (i % 5), "is_call": i % 3 != 0, **product}
    return {"trade_id": f"T{i}", "underlying": ["AAA", "BBB"][i % 2], "quantity": 10 - i, "product": p}


def book(n=8, **kw):
    return {"book_id": "B1", "market_data_version": "md-test", "trades": [trade(i) for i in range(n)],
            "simulation": {"n_paths": 20_000}, "task_size": 3, **kw}


def test_local_mode_matches_single_prices(make_client):
    with make_client() as c:
        out = c.post("/portfolio/price", json=book()).json()
        expected = 0.0
        for t in book()["trades"]:
            p = c.post("/price", json={"instrument_id": t["trade_id"], "market_data_version": "md-test",
                                       "product": t["product"], "simulation": {"n_paths": 20_000}}).json()
            expected += t["quantity"] * p["price"]
    assert out["mode"] == "local" and out["n_failed"] == 0 and out["tasks"] == 3
    assert out["pv"] == pytest.approx(expected, rel=1e-12)
    assert set(out["delta_by_underlying"]) == {"AAA"}  # European MC trades carry pathwise delta


def test_same_contract_errors_add_linearly_not_in_quadrature(make_client):
    same = trade(1)  # an Asian: Monte Carlo, so it has a standard error
    body = book(trades=[{**same, "trade_id": "x", "quantity": 3}, {**same, "trade_id": "y", "quantity": 4}])
    with make_client() as c:
        out = c.post("/portfolio/price", json=body).json()
        single = c.post("/price", json={"instrument_id": "x", "market_data_version": "md-test",
                                        "product": same["product"], "simulation": {"n_paths": 20_000}}).json()
    # Same contract -> same hash -> same seed -> perfectly correlated errors.
    assert out["pv_std_err"] == pytest.approx(7 * single["std_err"], rel=1e-9)
    assert out["pv_std_err"] > math.hypot(3, 4) * single["std_err"]


def test_analytic_trades_use_the_vectorised_path(make_client):
    trades = [{**trade(i), "product": {**trade(i)["product"], "type": "european", "method": "analytic"}}
              for i in range(6)]
    with make_client() as c:
        out = c.post("/portfolio/price", json=book(trades=trades, include_trades=True)).json()
    assert out["n_failed"] == 0 and out["pv_std_err"] == 0
    assert all(row["std_err"] == 0 for row in out["trades"])


def test_distributed_matches_local(make_client, r, workers):
    with make_client() as c:
        local = c.post("/portfolio/price", json=book(10)).json()
    workers(("w1", "w2", "w3"))
    with make_client(cache=PriceCache(r), queue=r) as c:
        dist = c.post("/portfolio/price", json={**book(10), "timeout_s": 20}).json()
    assert dist["mode"] == "distributed" and dist["n_failed"] == 0
    # Seeds come from the request hash, so it does not matter who priced what.
    assert dist["pv"] == pytest.approx(local["pv"], rel=1e-12)
    assert dist["tasks"] == 4 and sum(dist["workers_used"].values()) == 4


def test_bad_trade_fails_alone():
    task = {"md": "md", "sim": {"n_paths": 5_000}, "trades": [trade(0), {"trade_id": "bad", "product": {}}]}
    out = price_task(task, PriceCache(None), NullStore())
    assert out["n"] == 2 and out["n_failed"] == 1
    assert out["errors"]


def test_timeout_returns_partial(make_client, r):
    taskqueue.ensure_groups(r)  # no workers running
    with make_client(cache=PriceCache(r), queue=r) as c:
        resp = c.post("/portfolio/price", json={**book(4), "timeout_s": 0.2})
    assert resp.status_code == 206
    assert resp.json()["n_failed"] == 4


def test_intraday_is_served_before_batch(r):
    taskqueue.ensure_groups(r)
    taskqueue.submit(r, "exposure", "batch", [{"n": 1}], {})
    taskqueue.submit(r, "portfolio", "intraday", [{"n": 2}], {})
    (stream, _, fields), = taskqueue.read(r, "w", block_ms=10)
    assert stream == taskqueue.INTRADAY and fields[b"kind"] == b"portfolio"


def test_fifo_mode_uses_one_stream(r, monkeypatch):
    monkeypatch.setenv("QUEUE_MODE", "fifo")
    taskqueue.ensure_groups(r)
    taskqueue.submit(r, "exposure", "batch", [{"n": 1}], {})
    taskqueue.submit(r, "portfolio", "intraday", [{"n": 2}], {})
    (stream, _, fields), = taskqueue.read(r, "w", block_ms=10)
    assert stream == taskqueue.SHARED and fields[b"kind"] == b"exposure"  # first in, first out


def test_dead_worker_tasks_are_reclaimed(r, monkeypatch):
    monkeypatch.setattr(taskqueue, "CLAIM_IDLE_MS", 0)
    taskqueue.ensure_groups(r)
    task = {"md": "md", "sim": {"n_paths": 5_000}, "trades": [trade(0)]}
    job = taskqueue.submit(r, "portfolio", "intraday", [task], {})
    assert taskqueue.read(r, "dead", block_ms=10)  # read, never acked: a killed pod
    assert taskqueue.done(r, job) == 0
    Worker(r, PriceCache(r), NullStore(), consumer="alive").reclaim_once()
    assert taskqueue.results(r, job)[0]["n_failed"] == 0
    assert all(b["pending"] == 0 for b in taskqueue.backlog(r).values())


def test_poison_task_fails_after_max_deliveries(r, monkeypatch):
    monkeypatch.setattr(taskqueue, "CLAIM_IDLE_MS", 0)
    taskqueue.ensure_groups(r)
    job = taskqueue.submit(r, "portfolio", "intraday", [{"md": "md", "sim": {}, "trades": []}], {})
    taskqueue.read(r, "dead", block_ms=10)
    for i in range(taskqueue.MAX_DELIVERIES):  # each crash and reclaim adds a delivery
        r.xautoclaim(taskqueue.INTRADAY, taskqueue.GROUP, f"crash{i}", min_idle_time=0, start_id="0-0")
    Worker(r, PriceCache(r), NullStore(), consumer="alive").reclaim_once()
    assert taskqueue.results(r, job)[0]["error"] == "exceeded max deliveries"


def test_malformed_message_is_dropped_not_fatal(r):
    taskqueue.ensure_groups(r)
    r.xadd(taskqueue.INTRADAY, {"hello": "world"})  # e.g. added by hand with redis-cli
    w = Worker(r, PriceCache(r), NullStore(), consumer="w")
    assert w.run_once(block_ms=10) == 1  # handled, not raised
    assert all(b["pending"] == 0 for b in taskqueue.backlog(r).values())  # acked
