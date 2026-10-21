"""Distributed book pricing: scaling with workers and book size, and the
effect of task size (trades per queue message). Every run uses a fresh
market-data version, so nothing is served from cache.

Run (compose stack up): python bench/bench_portfolio.py
"""
import json
import os
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from books import book  # noqa: E402
from stack import API, up  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "results")


def price(trades, task_size=100, n_paths=20_000, persist=True):
    body = {"book_id": "BENCH", "market_data_version": f"md-{uuid.uuid4().hex[:8]}", "trades": trades,
            "simulation": {"n_paths": n_paths}, "task_size": task_size, "persist": persist, "timeout_s": 600}
    t0 = time.perf_counter()
    r = httpx.post(f"{API}/portfolio/price", json=body, timeout=700)
    wall = time.perf_counter() - t0
    r.raise_for_status()
    d = r.json()
    assert d["n_failed"] == 0, d["n_failed"]
    return {"wall_s": wall, "trades_per_s": len(trades) / wall, "workers": len(d["workers_used"]), "tasks": d["tasks"]}


def main():
    results = {"scaling": [], "task_size": []}
    books = {n: book(n, seed=n) for n in (1_000, 10_000, 50_000)}
    for workers in (1, 2, 4, 8):
        up(workers)
        price(books[1_000])  # warm-up
        for n, trades in books.items():
            if n == 50_000 and workers < 4:
                continue  # minutes per run; the trend is clear from the smaller books
            r = price(trades)
            results["scaling"].append({"book": n, **r})
            print(f"workers={workers} book={n:>6,} wall={r['wall_s']:6.2f}s  {r['trades_per_s']:8,.0f} trades/s")

    up(4)
    for ts in (1, 10, 50, 100, 500, 2_500):
        r = price(books[10_000], task_size=ts)
        results["task_size"].append({"task_size": ts, **r})
        print(f"task_size={ts:>5} tasks={r['tasks']:>6} wall={r['wall_s']:6.2f}s {r['trades_per_s']:8,.0f} trades/s")

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "portfolio.json"), "w") as f:
        json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
