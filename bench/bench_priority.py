"""Does a big batch run delay intraday pricing? Prices a 1,000-trade book
while a large exposure run is queued, with one FIFO stream versus separate
intraday and batch streams.

Run (compose stack up): python bench/bench_priority.py
"""
import json
import os
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from books import book  # noqa: E402
from netting_set import netting_set  # noqa: E402
from stack import API, up  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "results")


def price_book(trades):
    body = {"book_id": "INTRADAY", "market_data_version": f"md-{uuid.uuid4().hex[:8]}", "trades": trades,
            "simulation": {"n_paths": 20_000}, "task_size": 50, "timeout_s": 300}
    t0 = time.perf_counter()
    r = httpx.post(f"{API}/portfolio/price", json=body, timeout=400).json()
    assert r["n_failed"] == 0
    return time.perf_counter() - t0


def main():
    trades = book(1_000, seed=99)
    batch = netting_set(n_trades=200, n_scenarios=200_000, block_size=5_000)
    rows = []
    for mode in ("fifo", "priority"):
        up(4, QUEUE_MODE=mode)
        idle = price_book(trades)
        t0 = time.perf_counter()
        url = httpx.post(f"{API}/risk/exposure", json=batch, timeout=60).json()["status_url"]
        time.sleep(0.5)  # batch blocks are queued and running
        busy = price_book(trades)
        while httpx.get(f"{API}{url}", timeout=60).json()["status"] == "running":
            time.sleep(0.2)
        batch_s = time.perf_counter() - t0
        rows.append({"mode": mode, "intraday_idle_s": idle, "intraday_during_batch_s": busy, "batch_s": batch_s})
        print(f"{mode:8s} intraday idle={idle:5.2f}s  during batch={busy:6.2f}s  batch run={batch_s:6.2f}s")
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "priority.json"), "w") as f:
        json.dump(rows, f, indent=1)


if __name__ == "__main__":
    main()
