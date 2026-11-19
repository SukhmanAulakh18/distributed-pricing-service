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
    # One period, no discounting, EE 0 today and 1 at t = 1: the trapezoid
    # gives an interval average of 1/2, so CVA = (1 - R) (1/2) (1 - exp(-lambda)).
    lam = 0.01 / 0.6
    assert exposure.cva([1.0], [1.0], 0.0, 0.4, 100) == pytest.approx(0.6 * 0.5 * (1 - math.exp(-lam)))
    # A flat profile from today: the trapezoid and the exact integral agree.
    assert exposure.cva([1.0], [1.0], 0.0, 0.4, 100, ee0=1.0) == pytest.approx(0.6 * (1 - math.exp(-lam)))
    assert exposure.cva([1.0, 2.0], [0.0, 0.0], 0.05, 0.4, 500) == 0.0


def test_bilateral_cva_discounts_by_our_survival():
    # Over one period our own survival at the start is 1, so a single period
    # is unchanged; over two, the second is scaled by Q_own(1).
    one = exposure.cva([1.0, 2.0], [1.0, 1.0], 0.0, 0.4, 100, ee0=1.0)
    bil = exposure.cva([1.0, 2.0], [1.0, 1.0], 0.0, 0.4, 100, ee0=1.0, own=(0.4, 300))
    lam_c, lam_o = 0.01 / 0.6, 0.03 / 0.6
    p1, p2 = 1 - math.exp(-lam_c), math.exp(-lam_c) - math.exp(-2 * lam_c)
    assert one == pytest.approx(0.6 * (p1 + p2))
    assert bil == pytest.approx(0.6 * (p1 + p2 * math.exp(-lam_o)))


def test_epe_is_time_weighted():
    # Trapezoid from EE(0): (0+2)/2 * 1 + (2+5)/2 * 2 = 8, over 3 years.
    assert exposure.epe([1.0, 3.0], [2.0, 5.0]) == pytest.approx(8 / 3)
    assert exposure.epe([1.0, 3.0], [2.0, 2.0], ee0=2.0) == pytest.approx(2.0)


CSA = {"threshold_recv": 50, "threshold_post": 50, "mta": 5, "mpor_days": 10}


def test_tail_k_uses_exact_arithmetic():
    # 0.95 * 20_000 in floating point is 19_000.000000000004, whose ceiling
    # would make PFE one order statistic too low.
    assert ExposureRequest(**{**REQ, "n_scenarios": 20_000, "block_size": 5_000}).tail_k == 1_001


def test_validation_matches_the_core():
    with pytest.raises(ValueError, match="symmetric"):
        ExposureRequest(**{**REQ, "correlation": [[1, 0.6], [0.600001, 1]]})
    with pytest.raises(ValueError, match="positive definite"):
        ExposureRequest(**{**REQ, "correlation": [[1, 1.0], [1.0, 1]]})
    with pytest.raises(ValueError):
        ExposureRequest(**{**REQ, "underlyings": [{"name": "SPX", "spot": float("inf"), "vol": 0.2},
                                                  REQ["underlyings"][1]]})


def test_csa_grid_adds_a_node_one_mpor_before_each_date():
    req = ExposureRequest(**{**REQ, "csa": CSA})
    nodes, lag, report = req.sim_grid
    assert [nodes[i] for i in report] == req.grid
    d = 10 / 252  # business days
    for j, t in enumerate(nodes):
        if lag[j] >= 0:
            assert nodes[lag[j]] <= t - d + 1e-9
            assert lag[j] == len(nodes) - 1 or nodes[lag[j] + 1] > t - d + 1e-9
        else:  # no node at or before t - MPoR: the balance held is today's
            assert nodes[0] > t - d + 1e-9
    assert all(abs(nodes[lag[report[i]]] - (t - d)) < 1e-9 for i, t in enumerate(req.grid))


def test_seed_ignores_trades_credit_and_csa_amounts():
    """Common random numbers: what-if runs differ only by what changed."""
    seed = exposure.plan(ExposureRequest(**REQ))[0]["seed"]
    more = {**REQ, "trades": REQ["trades"][:2], "cds_spread_bps": 300, "own_credit": {"cds_spread_bps": 80}}
    assert exposure.plan(ExposureRequest(**more))[0]["seed"] == seed
    # Market levels are bumped for sensitivities: they must share scenarios too.
    bumped = {**REQ, "rate": 0.03, "underlyings": [{**REQ["underlyings"][0], "spot": 101, "vol": 0.21},
                                                   REQ["underlyings"][1]]}
    assert exposure.plan(ExposureRequest(**bumped))[0]["seed"] == seed
    assert exposure.plan(ExposureRequest(**{**REQ, "market_data_version": "md2"}))[0]["seed"] != seed
    # Thresholds and MTA share scenarios; the MPoR adds nodes, so it does not.
    a = exposure.plan(ExposureRequest(**{**REQ, "csa": CSA}))[0]["seed"]
    b = exposure.plan(ExposureRequest(**{**REQ, "csa": {**CSA, "threshold_recv": 0, "mta": 0}}))[0]["seed"]
    c = exposure.plan(ExposureRequest(**{**REQ, "csa": {**CSA, "mpor_days": 5}}))[0]["seed"]
    assert a == b != c


@pytest.mark.parametrize("csa,extra", [
    ({"mpor_days": 0}, {}),
    ({"mpor_days": 60}, {"horizon_years": 0.1, "step_months": 1}),
    ({"mpor_days": 10}, {"step_months": 12, "horizon_years": 3.0}),
    ({"mpor_days": 10}, {"pfe_quantile": 0.999, "n_scenarios": 5_000, "block_size": 1_700}),
])
def test_csa_edge_cases_are_finite_and_bounded(make_client, csa, extra):
    body = {**REQ, **extra, "csa": {**CSA, **csa}, "own_credit": {}}
    with make_client() as c:
        res = c.get(c.post("/risk/exposure", json=body).json()["status_url"]).json()
    assert res["status"] == "complete"
    for k in ("ee", "pfe", "ene", "ee_csa", "pfe_csa"):
        assert all(math.isfinite(x) and x >= 0 for x in res[k]), k
    assert all(math.isfinite(res[k]) for k in ("cva", "dva", "cva_csa", "dva_csa", "bilateral_cva_csa"))


def test_old_exposure_job_still_assembles():
    """A job queued before CSA support: no sim_grid, report, own or csa in
    its meta, and blocks without neg_sum or v0 (old workers)."""
    from service.main import _exposure_result
    req = ExposureRequest(**REQ)
    payload, tasks = exposure.plan(req)
    meta, blob = exposure.run_block(payload, 0, req.n_scenarios, 2)
    old_block = ({k: meta[k] for k in ("sum", "sum_sq", "n", "k")}, blob)
    old_meta = {"netting_set_id": "NS-1", "md": "md", "grid": req.grid, "tail_k": req.tail_k, "rate": req.rate,
                "recovery": req.recovery, "spread_bps": req.cds_spread_bps}
    res = _exposure_result("old", old_meta, [old_block], {}, 0.0)
    assert res.pfe == exposure.merge([(meta, blob)], req.grid, req.tail_k)["pfe"]
    assert res.cva > 0 and res.dva is None


def test_csa_lowers_exposure_and_cva(make_client):
    with make_client() as c:
        res = c.get(c.post("/risk/exposure", json={**REQ, "csa": CSA, "own_credit": {}}).json()["status_url"]).json()
    assert res["status"] == "complete"
    assert len(res["ee_csa"]) == len(res["pfe_csa"]) == len(res["grid"]) == 24
    assert 0 < res["cva_csa"] < res["cva"]
    assert res["peak_pfe_csa"] < res["peak_pfe"]
    assert all(0 <= se < 0.05 * max(res["ee"]) for se in res["ee_se"])
    # Bilateral CVA nets DVA off a CVA that also requires us to survive.
    assert res["dva"] > 0 and res["bilateral_cva"] < res["cva"] - res["dva"] + 1e-12


def test_csa_with_unreachable_thresholds_changes_nothing(make_client):
    loose = {**CSA, "threshold_recv": 1e12, "threshold_post": 1e12}
    with make_client() as c:
        res = c.get(c.post("/risk/exposure", json={**REQ, "csa": loose}).json()["status_url"]).json()
    assert res["ee_csa"] == res["ee"] and res["pfe_csa"] == res["pfe"]
    assert res["cva_csa"] == pytest.approx(res["cva"], rel=1e-12)


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


def test_distributed_csa_run_matches_local(make_client, r, workers):
    req = {**REQ, "csa": CSA, "own_credit": {}}
    with make_client() as c:
        local = c.get(c.post("/risk/exposure", json=req).json()["status_url"]).json()
    workers(("w1", "w2"))
    with make_client(cache=PriceCache(r), queue=r) as c:
        url = c.post("/risk/exposure", json=req).json()["status_url"]
        deadline = time.time() + 30
        while (res := c.get(url).json())["status"] == "running" and time.time() < deadline:
            time.sleep(0.05)
    assert res["status"] == "complete"
    assert res["pfe_csa"] == local["pfe_csa"]
    assert res["bilateral_cva_csa"] == pytest.approx(local["bilateral_cva_csa"], rel=1e-12)


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


def test_blocks_from_mixed_worker_versions_fail_loudly():
    req = ExposureRequest(**{**REQ, "csa": CSA})
    payload, _ = exposure.plan(req)
    new = exposure.run_block(payload, 0, 3_000, 2)
    old = exposure.run_block({**payload, "csa": None}, 3_000, 3_000, 2)
    with pytest.raises(ValueError, match="mixed worker versions"):
        exposure.merge([new, old], payload["grid"], req.tail_k, payload["report"])
