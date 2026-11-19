import pytest

from service.main import _exposure_inline
from service.models import ExposureRequest
from tests.test_exposure import CSA, REQ

NEW = {"trade_id": "new", "underlying": "SPX", "kind": "forward", "strike": 100, "maturity": 2, "quantity": 30}


def predeal(c, **kw):
    body = {"netting_set": REQ, "new_trade": NEW} | kw
    r = c.post("/risk/predeal", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_incremental_cva_is_the_difference_on_the_same_scenarios(make_client):
    with make_client() as c:
        res = predeal(c)
    before = _exposure_inline(ExposureRequest(**REQ))
    after = _exposure_inline(ExposureRequest(**{**REQ, "trades": [*REQ["trades"], NEW]}))
    # Common random numbers: exactly the difference of the two runs, not a noisy estimate of it.
    assert res["incremental_cva"] == pytest.approx(after.cva - before.cva, rel=1e-12)
    assert res["peak_pfe_before"] == before.peak_pfe and res["peak_pfe_after"] == after.peak_pfe
    assert res["verdict"] == "no_limit" and res["limit_utilisation"] is None
    assert res["cva_charge_bps"] == pytest.approx(res["incremental_cva"] / (30 * 100) * 1e4)


def test_verdict_follows_the_limit(make_client):
    with make_client() as c:
        unc = predeal(c)["peak_pfe_after"]
        coll = predeal(c, credit_limit=1.0, fallback_csa=CSA)["peak_pfe_after_csa"]  # breached: CSA run happens
        assert coll < unc
        fits = predeal(c, credit_limit=unc * 1.01, fallback_csa=CSA)
        assert fits["verdict"] == "approve" and fits["peak_pfe_after_csa"] is None  # not needed, not run
        mid = predeal(c, credit_limit=(unc + coll) / 2, fallback_csa=CSA)
        assert mid["verdict"] == "approve_with_csa" and "proposed CSA" in mid["reason"]
        assert predeal(c, credit_limit=(unc + coll) / 2)["verdict"] == "refer_to_credit"  # no CSA offered
        low = predeal(c, credit_limit=coll / 2, fallback_csa=CSA)
        assert low["verdict"] == "refer_to_credit" and "even with collateral" in low["reason"]


def test_netting_set_with_a_csa_is_judged_collateralized(make_client):
    with make_client() as c:
        res = predeal(c, netting_set={**REQ, "csa": CSA}, credit_limit=1e9)
    assert res["limit_basis"] == "collateralized" and res["verdict"] == "approve"
    assert res["incremental_cva"] == res["incremental_cva_csa"]
    assert res["peak_pfe_after"] == res["peak_pfe_after_csa"]


def test_offsetting_trade_is_risk_reducing(make_client):
    hedge = {**REQ["trades"][0], "trade_id": "hedge", "quantity": -REQ["trades"][0]["quantity"]}
    with make_client() as c:
        res = predeal(c, new_trade=hedge)
    assert res["incremental_cva"] < 0 and res["risk_reducing"]


@pytest.mark.parametrize("change,match", [
    ({"netting_set": {**REQ, "n_scenarios": 60_000}}, "synchronously"),
    ({"new_trade": {**NEW, "underlying": "NKY"}}, "unknown underlying"),
    ({"new_trade": {**NEW, "trade_id": "c1"}}, "already in the netting set"),
    ({"netting_set": {**REQ, "csa": CSA}, "fallback_csa": CSA}, "fallback_csa"),
])
def test_rejects_bad_requests(make_client, change, match):
    with make_client() as c:
        r = c.post("/risk/predeal", json={"netting_set": REQ, "new_trade": NEW} | change)
    assert r.status_code == 422 and match in r.text


def test_book_baseline_is_cached_between_quotes(make_client, r):
    from service.cache import PriceCache
    with make_client(cache=PriceCache(r)) as c:
        first, second = predeal(c), predeal(c, new_trade={**NEW, "quantity": 60})
    assert not first["baseline_cached"] and second["baseline_cached"]
    assert second["peak_pfe_before"] == first["peak_pfe_before"]
    with make_client(cache=PriceCache(r)) as c:  # a different book misses
        assert not predeal(c, netting_set={**REQ, "rate": 0.03})["baseline_cached"]


def test_csa_verdict_is_priced_under_the_proposed_csa(make_client):
    with make_client() as c:
        unc = predeal(c)["peak_pfe_after"]
        coll = predeal(c, credit_limit=1.0, fallback_csa=CSA)["peak_pfe_after_csa"]
        res = predeal(c, credit_limit=(unc + coll) / 2, fallback_csa=CSA)
    assert res["verdict"] == "approve_with_csa" and res["charge_basis"] == "proposed_csa"
    before = _exposure_inline(ExposureRequest(**{**REQ, "csa": CSA}))
    after = _exposure_inline(ExposureRequest(**{**REQ, "csa": CSA, "trades": [*REQ["trades"], NEW]}))
    assert res["incremental_cva"] == pytest.approx(after.cva_csa - before.cva_csa, rel=1e-12)
    assert res["incremental_cva"] < res["incremental_cva_uncollateralized"]  # collateral makes credit cheaper
    assert "Under that CSA" in res["reason"]


def test_running_spread_is_upfront_over_the_risky_annuity(make_client):
    import math
    with make_client() as c:
        res = predeal(c)
    lam, dt = 100 / 1e4 / 0.6, 2 / 24  # REQ: 100 bp, 40% recovery; NEW: 2-year trade, monthly
    annuity = sum(math.exp(-0.02 * i * dt) * math.exp(-lam * i * dt) * dt for i in range(1, 25))
    assert res["cva_charge_running_bps"] == pytest.approx(res["cva_charge_bps"] / annuity)
    assert res["cva_charge_running_bps"] < res["cva_charge_bps"]  # spread a year < upfront for a 2y trade


def test_hedge_is_a_benefit_not_a_negative_charge(make_client):
    hedge = {**REQ["trades"][0], "trade_id": "hedge", "quantity": -REQ["trades"][0]["quantity"]}
    with make_client() as c:
        res = predeal(c, new_trade=hedge)
    assert "Credit benefit" in res["reason"] and "$-" not in res["reason"]
