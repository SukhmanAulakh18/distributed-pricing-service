"""Collateral and XVA on the benchmark netting set, in process (no stack).

What a CSA is worth for one client book: CVA, DVA and peak PFE with no
collateral, across thresholds and margin periods, and how much common random
numbers tighten a pre-deal incremental CVA. Uses the service's own plan, C++
blocks and result assembly, so these are the numbers the API returns.

Run: python bench/bench_xva.py  ->  bench/results/xva.json
"""
import json
import os
import platform
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from netting_set import netting_set  # noqa: E402

from service import exposure  # noqa: E402
from service.main import _exposure_result, exposure_meta  # noqa: E402
from service.models import ExposureRequest  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "results", "xva.json")
THREADS = os.cpu_count() or 4
CPTY = {"recovery": 0.4, "cds_spread_bps": 150.0}  # a BBB-ish corporate
OWN = {"recovery": 0.4, "cds_spread_bps": 60.0}  # a bank


def run(body: dict) -> tuple[dict, float]:
    req = ExposureRequest(**body)
    payload, _ = exposure.plan(req)
    t0 = time.perf_counter()
    block = exposure.run_block(payload, 0, req.n_scenarios, THREADS)
    res = _exposure_result("bench", exposure_meta(req, payload), [block], {}, 0.0).model_dump()
    return res, time.perf_counter() - t0


def summary(res: dict, sfx: str = "") -> dict:
    return {k: round(res[k + sfx], 2) for k in ("cva", "peak_pfe", "epe")} | (
        {"dva": round(res["dva" + sfx], 2), "bilateral_cva": round(res["bilateral_cva" + sfx], 2)}
        if res.get("dva" + sfx) is not None else {})


SCALE = 1_000  # each benchmark contract is on 1,000 units, so amounts read in dollars


def main():
    base = netting_set(n_trades=200, n_scenarios=50_000, block_size=50_000) | CPTY | {"own_credit": OWN}
    base["trades"] = [t | {"quantity": t["quantity"] * SCALE} for t in base["trades"]]
    out = {"machine": f"{platform.machine()}, {THREADS} threads", "n_trades": 200, "n_scenarios": 50_000,
           "units": "USD; 1,000 units per contract", "counterparty": CPTY, "own_credit": OWN}

    res, secs = run(base)
    out["uncollateralized"] = summary(res) | {"seconds": round(secs, 2), "v0": round(res["v0"], 2)}
    out["profile"] = {"grid": res["grid"], "ee": [round(x, 2) for x in res["ee"]],
                      "pfe": [round(x, 2) for x in res["pfe"]], "ene": [round(x, 2) for x in res["ene"]]}
    print("uncollateralized", out["uncollateralized"])

    # Threshold sweep at the regulatory 10-day margin period.
    out["thresholds"] = []
    for h in (0, 1_000_000, 5_000_000, 20_000_000, 100_000_000):
        csa = {"threshold_recv": h, "threshold_post": h, "mta": 250_000, "mpor_days": 10}
        res, secs = run(base | {"csa": csa})
        # The uncollateralized numbers from the same scenarios, for a like-for-like saving.
        row = {"threshold": h} | summary(res, "_csa") | {"seconds": round(secs, 2),
                                                          "same_scenarios_uncollateralized": summary(res)}
        out["thresholds"].append(row)
        print("threshold", row)
        if h == 0:
            out["profile"] |= {"ee_csa": [round(x, 2) for x in res["ee_csa"]],
                               "pfe_csa": [round(x, 2) for x in res["pfe_csa"]]}

    # Margin period sweep with a zero threshold: what slower margining costs.
    out["mpor"] = []
    for d in (0, 2, 5, 10, 20):
        res, _ = run(base | {"csa": {"threshold_recv": 0, "threshold_post": 0, "mta": 250_000, "mpor_days": d}})
        out["mpor"].append({"mpor_days": d} | summary(res, "_csa") | {"same_scenarios_uncollateralized": summary(res)})
        print("mpor", out["mpor"][-1])

    # Pre-deal check: the CVA a new 5-year forward adds to the book. With the
    # seed shared (same market data), both runs see the same scenarios and the
    # difference is clean; with independent seeds each estimate's noise lands
    # in the difference. Repeated over 6 market-data versions to measure the
    # spread of each estimator.
    new = {"trade_id": "NEW", "underlying": "SPX", "kind": "forward", "strike": 100.0, "maturity": 5.0,
           "quantity": 40 * SCALE}
    small = base | {"n_scenarios": 10_000, "block_size": 10_000}
    crn, indep, secs_pre = [], [], []
    for i in range(6):
        md, md2 = f"md-{i}", f"md-{i}-b"
        before, _ = run(small | {"market_data_version": md})
        after, s = run(small | {"market_data_version": md, "trades": small["trades"] + [new]})
        after2, _ = run(small | {"market_data_version": md2, "trades": small["trades"] + [new]})
        crn.append(after["cva"] - before["cva"])
        indep.append(after2["cva"] - before["cva"])
        secs_pre.append(s)
    out["pre_deal"] = {
        "trade": new, "n_scenarios": 10_000,
        "incremental_cva_mean": round(statistics.mean(crn), 2),
        "stdev_common_random_numbers": round(statistics.stdev(crn), 3),
        "stdev_independent_seeds": round(statistics.stdev(indep), 3),
        "seconds_per_run": round(statistics.median(secs_pre), 2),
    }
    print("pre-deal", out["pre_deal"])
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
