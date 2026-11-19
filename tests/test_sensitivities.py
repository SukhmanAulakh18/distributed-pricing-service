"""CVA sensitivities against an independent closed form.

For one long forward, E[max(V(t), 0)] is a Black formula in S(t), so the
uncollateralized CVA has a closed form on the same grid, and so do its
central bumps. The engine's bump-and-revalue reuses one set of scenarios
for every bump, so its delta and vega land within a fraction of a percent
of the closed form, much closer than its CVA level does.
"""
import math

import pytest

from service import exposure

R_, Q_, K_, T_, QTY, S_, VOL, REC, SPR = 0.02, 0.0, 100.0, 2.0, 10.0, 100.0, 0.25, 0.4, 150.0
GRID = [(i + 1) / 12 for i in range(24)]
NS = {"netting_set_id": "F", "market_data_version": "m", "rate": R_,
      "underlyings": [{"name": "X", "spot": S_, "vol": VOL}],
      "trades": [{"trade_id": "f", "underlying": "X", "kind": "forward", "strike": K_, "maturity": T_, "quantity": QTY}],
      "horizon_years": 2.0, "n_scenarios": 50_000, "block_size": 50_000, "cds_spread_bps": SPR, "recovery": REC}


def ee_forward(s, vol, t):
    if t > T_ + 1e-12:
        return 0.0
    tau = T_ - t
    a, c, sd = math.exp(-Q_ * tau), K_ * math.exp(-R_ * tau), vol * math.sqrt(t)
    d1 = (math.log(a * s / c) + (R_ - Q_) * t) / sd + 0.5 * sd
    n = lambda x: 0.5 * math.erfc(-x / math.sqrt(2))  # noqa: E731
    return QTY * (a * s * math.exp((R_ - Q_) * t) * n(d1) - c * n(d1 - sd))


def cva_closed(s, vol, spread=SPR):
    v0 = QTY * (s * math.exp(-Q_ * T_) - K_ * math.exp(-R_ * T_))
    return exposure.cva(GRID, [ee_forward(s, vol, t) for t in GRID], R_, REC, spread, ee0=max(v0, 0.0))


@pytest.fixture(scope="module")
def res():
    from fastapi.testclient import TestClient

    from service.cache import PriceCache
    from service.main import app
    from service.store import NullStore
    app.state.cache, app.state.store, app.state.queue = PriceCache(None), NullStore(), None
    with TestClient(app) as c:
        r = c.post("/risk/cva-sensitivities", json={"netting_set": NS})
    assert r.status_code == 200, r.text
    return r.json()


def test_delta_and_vega_match_the_closed_form(res):
    u = res["underlyings"][0]
    delta = (cva_closed(S_ * 1.01, VOL) - cva_closed(S_ * 0.99, VOL)) / 2
    vega = (cva_closed(S_, VOL + 0.01) - cva_closed(S_, VOL - 0.01)) / 2
    assert u["delta"] == pytest.approx(delta, rel=0.01)
    assert u["vega"] == pytest.approx(vega, rel=0.02)
    assert res["cva"] == pytest.approx(cva_closed(S_, VOL), rel=0.02)  # the level is noisier than its bumps
    assert res["n_runs"] == 5 and res["basis"] == "uncollateralized"


def test_cs01_needs_no_new_simulation(res):
    # The spread does not move the scenarios, so CS01 is exact on the base run's EE.
    assert res["cs01"] > 0
    ratio = res["cs01"] / res["cva"]
    assert ratio == pytest.approx((cva_closed(S_, VOL, SPR + 1) - cva_closed(S_, VOL)) / cva_closed(S_, VOL), rel=0.01)


def test_rejects_requests_too_big_to_run_synchronously(make_client):
    with make_client() as c:
        big = c.post("/risk/cva-sensitivities", json={"netting_set": {**NS, "n_scenarios": 60_000}})
        low_vol = c.post("/risk/cva-sensitivities", json={"netting_set": NS, "vol_bump": 0.3})
    assert big.status_code == 422 and "synchronously" in big.text
    assert low_vol.status_code == 422 and "vol_bump" in low_vol.text
