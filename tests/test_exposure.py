import math
import time

import numpy as np
import pytest

from service import exposure, taskqueue
from service.cache import PriceCache
from service.models import ExposureRequest

REQ = {
    "netting_set_id": "NS-1", "market_data_version": "md", "rate": 0.02,
    "underlyings": [{"name": "SPX", "spot": 100, "vol": 0.2}, {"name": "TSX", "spot": 50, "vol": 0.3, "div_yield": 0.02}],
    "correlation": [[1, 0.6], [0.6, 1]],
    "trades": [
        {"trade_id": "c1", "underlying": "SPX", "kind": "call", "strike": 100, "maturity": 2, "quantity": 10},
        {"trade_id": "p1", "underlying": "TSX", "kind": "put", "strike": 55, "maturity": 1.5, "quantity": -20},
        {"trade_id": "f1", "underlying": "SPX", "kind": "forward", "strike": 98, "maturity": 1, "quantity": -5},
    ],
    "horizon_years": 2.0, "n_scenarios": 6_000, "block_size": 1_500,
}


def test_blocks_merge_to_the_single_run():
    req = ExposureRequest(**REQ)
    payload, tasks = exposure.plan(req)
    assert len(tasks) == 4
    blocks = [exposure.run_block(payload, t["start"], t["count"], 2) for t in tasks]
    whole = exposure.run_block(payload, 0, req.n_scenarios, 4)
    a, b = exposure.merge(blocks, req.grid, req.tail_k), exposure.merge([whole], req.grid, req.tail_k)
    assert a["pfe"] == b["pfe"]  # order statistics: bit-identical
    assert a["ee"] == pytest.approx(b["ee"], rel=1e-12)


def test_pfe_is_the_quantile_of_the_exposure_distribution():
    req = ExposureRequest(**{**REQ, "n_scenarios": 2_000, "block_size": 2_000})
    payload, _ = exposure.plan(req)
    payload["tail_k"] = 2_000  # keep every scenario so we can check against numpy
    meta, blob = exposure.run_block(payload, 0, 2_000, 2)
    every = np.frombuffer(blob).reshape(len(req.grid), 2_000)
    pfe = exposure.merge([(meta, blob)], req.grid, req.tail_k)["pfe"]
    for j in range(len(req.grid)):
        assert pfe[j] == np.sort(every[j])[math.ceil(0.95 * 2_000) - 1]


def test_cva_hand_calculation():
    # One period, EE = 1, no discounting: CVA = (1 - R) (1 - exp(-lambda)).
    lam = 0.01 / 0.6
    assert exposure.cva([1.0], [1.0], 0.0, 0.4, 100) == pytest.approx(0.6 * (1 - math.exp(-lam)))
    assert exposure.cva([1.0, 2.0], [0.0, 0.0], 0.05, 0.4, 500) == 0.0


def test_epe_is_time_weighted():
    assert exposure.epe([1.0, 3.0], [2.0, 5.0]) == pytest.approx((2 * 1 + 5 * 2) / 3)


def test_local_run_via_api(make_client):
    with make_client() as c:
        job = c.post("/risk/exposure", json=REQ)
        assert job.status_code == 202
        res = c.get(job.json()["status_url"]).json()
    assert res["status"] == "complete" and res["n_scenarios"] == 6_000
    assert len(res["ee"]) == len(res["pfe"]) == 24
    assert res["cva"] > 0 and res["peak_pfe"] == max(res["pfe"])


def test_distributed_run_matches_local(make_client, r, workers):
    with make_client() as c:
        local = c.get(c.post("/risk/exposure", json=REQ).json()["status_url"]).json()
    workers(("w1", "w2"))
    with make_client(cache=PriceCache(r), queue=r) as c:
        url = c.post("/risk/exposure", json=REQ).json()["status_url"]
        deadline = time.time() + 30
        while (res := c.get(url).json())["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
    assert res["status"] == "complete"
    assert res["pfe"] == local["pfe"]
    assert res["cva"] == pytest.approx(local["cva"], rel=1e-12)
    assert sum(res["workers_used"].values()) == 4


def test_missing_payload_fails_the_job(make_client, r, workers):
    with make_client(cache=PriceCache(r), queue=r) as c:
        url = c.post("/risk/exposure", json=REQ).json()["status_url"]
        job_id = url.rsplit("/", 1)[1]
        r.delete(f"job:{job_id}:payload")  # e.g. expired before a worker got to it
        workers(("w1",))
        deadline = time.time() + 30
        while (res := c.get(url).json())["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
    assert res["status"] == "failed"
    assert "payload" in res["errors"][0]
    assert taskqueue.meta(r, job_id)["kind"] == "exposure"
