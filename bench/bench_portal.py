"""Grid behind the client hedging portal (docs/portal.html).

A corporate treasurer sells USD forward to the bank. For each notional x tenor x
CSA setting, this calls the real POST /risk/predeal (FastAPI TestClient, as in
bench_xva.py) and stores the answer. The page only looks numbers up: nothing is
priced in the browser, so it cannot drift from the engine (tests/test_portal_data.py
re-runs a cell to check).

Everything is synthetic. Money is CAD. The engine's forward is a Black-Scholes
forward with a dividend yield, which with the USD rate as the yield is
Garman-Kohlhagen for USD/CAD.

Run: python bench/bench_portal.py  ->  bench/results/portal.json and docs/data/portal.json
"""
import json
import math
import os
import platform
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from service.cache import PriceCache  # noqa: E402
from service.main import app  # noqa: E402
from service.store import NullStore  # noqa: E402

HERE = os.path.dirname(__file__)
OUTS = [os.path.join(HERE, "results", "portal.json"), os.path.join(HERE, "..", "docs", "data", "portal.json")]

NOTIONALS = [1_000_000, 5_000_000, 10_000_000, 25_000_000]  # USD sold forward
TENORS = [0.5, 1.0, 2.0, 3.0, 5.0]  # years
SPOT, R_CAD, R_USD, VOL = 1.38, 0.0275, 0.04, 0.07
CPTY = {"recovery": 0.4, "cds_spread_bps": 150.0}
LIMIT = 2_000_000  # CAD, on peak PFE
CSA = {"threshold_recv": 0, "threshold_post": 0, "mta": 50_000, "mpor_days": 10}
N_SCENARIOS = 10_000
MD_VERSION = "portal-md-1"
# The API caps a trade's quantity at 1e7 units, so one unit is USD 1,000 (spot
# CAD 1,380 per unit): the same money, and a 25M USD trade fits.
UNIT = 1_000
PRIOR_USD, PRIOR_T = 500_000, 0.5  # the client's existing small forward


def forward(t: float) -> float:
    return SPOT * math.exp((R_CAD - R_USD) * t)


def body(notional: float, tenor: float, csa: bool) -> dict:
    ns = {
        "netting_set_id": "CLIENT-1", "market_data_version": MD_VERSION, "rate": R_CAD,
        "underlyings": [{"name": "USDCAD", "spot": SPOT * UNIT, "vol": VOL, "div_yield": R_USD}],
        "trades": [{"trade_id": "PRIOR", "underlying": "USDCAD", "kind": "forward",
                    "strike": forward(PRIOR_T) * UNIT, "maturity": PRIOR_T, "quantity": PRIOR_USD / UNIT}],
        "horizon_years": tenor, "step_months": 1, "n_scenarios": N_SCENARIOS, "block_size": N_SCENARIOS,
        "pfe_quantile": 0.95, **CPTY,
    }
    if csa:
        ns["csa"] = CSA
    new = {"trade_id": "HEDGE", "underlying": "USDCAD", "kind": "forward", "strike": forward(tenor) * UNIT,
           "maturity": tenor, "quantity": notional / UNIT}
    return {"netting_set": ns, "new_trade": new, "credit_limit": LIMIT} | ({} if csa else {"fallback_csa": CSA})


def cell(res: dict, notional: float, tenor: float) -> dict:
    keep = ("verdict", "reason", "incremental_cva", "cva_charge_bps", "cva_charge_running_bps",
            "peak_pfe_after", "peak_pfe_after_csa", "limit_utilisation")
    out = {"forward_rate": round(forward(tenor), 4)} | {
        k: (round(res[k], 4) if isinstance(res[k], float) else res[k]) for k in keep}
    # When a limit breach is cured by the proposed CSA, the API's headline charge
    # is the CSA one (the terms the trade would be done on). The page's "no CSA"
    # number must stay the uncollateralized one, so keep it too. Running = upfront
    # over the risky annuity, and the API's running/upfront ratio is 1 / annuity.
    unc_upfront_bps = res["incremental_cva_uncollateralized"] / (notional * SPOT) * 1e4
    ratio = res["cva_charge_running_bps"] / res["cva_charge_bps"]
    return out | {"charge_basis": res["charge_basis"],
                  "uncollateralized_cva": round(res["incremental_cva_uncollateralized"], 4),
                  "uncollateralized_running_bps": round(unc_upfront_bps * ratio, 4)}


def quote(c: TestClient, notional: float, tenor: float, csa: bool) -> dict:
    r = c.post("/risk/predeal", json=body(notional, tenor, csa))
    assert r.status_code == 200, r.text
    return r.json()


def main():
    app.state.store, app.state.cache, app.state.queue = NullStore(), PriceCache(None), None
    t0 = time.perf_counter()
    grid = {}
    with TestClient(app) as c:
        for n in NOTIONALS:
            for t in TENORS:
                for csa in (False, True):
                    grid[f"{n}|{t}|{'on' if csa else 'off'}"] = cell(quote(c, n, t, csa), n, t)
                print(n, t, grid[f"{n}|{t}|off"]["verdict"], grid[f"{n}|{t}|on"]["verdict"], flush=True)
    secs = time.perf_counter() - t0
    out = {
        "generated_by": "bench/bench_portal.py, calling POST /risk/predeal in process (FastAPI TestClient)",
        "machine": f"{platform.machine()}, {os.cpu_count()} cpus", "seconds": round(secs, 1),
        "synthetic": True, "currency": "CAD",
        "inputs": {
            "pair": "USD/CAD", "spot_cad_per_usd": SPOT, "cad_rate": R_CAD, "usd_rate": R_USD, "vol": VOL,
            "forward_rate": "spot * exp((cad_rate - usd_rate) * tenor), the at-market forward (V0 about 0)",
            "counterparty": CPTY | {"description": "synthetic BBB-like corporate"},
            "prior_forward": {"usd": PRIOR_USD, "maturity_years": PRIOR_T},
            "credit_limit_cad_on_peak_pfe": LIMIT, "csa": CSA | {"mpor_days_are": "business days"},
            "n_scenarios": N_SCENARIOS, "market_data_version": MD_VERSION,
            "engine_unit": f"one engine unit = USD {UNIT:,} (the API caps quantity at 1e7 units)",
        },
        "notionals_usd": NOTIONALS, "tenors_years": TENORS, "cells": grid,
    }
    for p in OUTS:
        with open(p, "w") as f:
            json.dump(out, f, indent=1)
            f.write("\n")
    print(f"{len(grid)} cells in {secs:.1f}s")


if __name__ == "__main__":
    main()
