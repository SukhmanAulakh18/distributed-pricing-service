"""Distributed exposure run: scaling with workers, plus a check that the
answer is identical whatever the worker count.

Run (compose stack up): python bench/bench_exposure.py
"""
import json
import os
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from netting_set import netting_set  # noqa: E402
from stack import API, up  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "results")


def run(body):
    t0 = time.perf_counter()
    url = httpx.post(f"{API}/risk/exposure", json=body, timeout=60).json()["status_url"]
    while (res := httpx.get(f"{API}{url}", timeout=60).json())["status"] == "running":
        time.sleep(0.1)
    assert res["status"] == "complete", res
    return time.perf_counter() - t0, res


def main():
    body = netting_set(n_trades=200, n_scenarios=100_000)
    grid = [(i + 1) / 12 for i in range(60)]
    # Count only revaluations of live trades: expired ones cost nothing.
    live = sum(sum(1 for t in grid if t < tr["maturity"]) for tr in body["trades"])
    revals = body["n_scenarios"] * live
    rows, reference = [], None
    for workers in (1, 2, 4, 8):
        up(workers)
        wall, res = run(body)
        reference = reference or res
        same = res["pfe"] == reference["pfe"] and res["cva"] == reference["cva"]
        rows.append({"workers": workers, "wall_s": wall, "revals_per_s": revals / wall, "identical": same})
        print(f"workers={workers} wall={wall:6.2f}s  {revals / wall / 1e6:7.1f}M option revaluations/s  identical={same}")
    print(f"CVA={reference['cva']:.2f}  EPE={reference['epe']:.2f}  peak PFE95={reference['peak_pfe']:.2f}")
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "exposure.json"), "w") as f:
        json.dump({"revaluations": revals, "scaling": rows, "profile": reference}, f, indent=1)


if __name__ == "__main__":
    main()
