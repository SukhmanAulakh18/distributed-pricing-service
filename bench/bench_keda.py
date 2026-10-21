"""KEDA scale-from-zero: what an exposure run pays when the batch pool is at
zero, versus a warm pool. Records the batch pool's ready replicas over time.

Run (kind cluster, batch pool idle at 0): python bench/bench_keda.py
"""
import json
import os
import subprocess
import sys
import time

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from netting_set import netting_set  # noqa: E402

API = "http://localhost:8080"
OUT = os.path.join(os.path.dirname(__file__), "results")


def ready() -> int:
    out = subprocess.run(["kubectl", "-n", "pricing", "get", "deploy", "pricer-worker-batch", "-o",
                          "jsonpath={.status.readyReplicas}"], capture_output=True, text=True).stdout
    return int(out or 0)


def run(label: str) -> dict:
    body = netting_set(n_trades=150, n_scenarios=200_000, block_size=5_000)
    t0 = time.time()
    url = httpx.post(f"{API}/risk/exposure", json=body, timeout=30).json()["status_url"]
    timeline, first_ready = [], None
    while (res := httpx.get(f"{API}{url}", timeout=30).json())["status"] == "running":
        n = ready()
        timeline.append((round(time.time() - t0, 1), n))
        if n and first_ready is None:
            first_ready = time.time() - t0
        time.sleep(1)
    total = time.time() - t0
    print(f"{label:10s} start replicas={timeline[0][1] if timeline else '?'}  first worker ready {first_ready or 0:5.1f}s  "
          f"run {total:5.1f}s  peak replicas {max(n for _, n in timeline) if timeline else '?'}")
    return {"label": label, "first_ready_s": first_ready, "total_s": total, "timeline": timeline}


def main():
    print("waiting for the batch pool to be at zero...")
    while ready():
        time.sleep(5)
    cold = run("cold")
    warm = run("warm")  # straight after: the pool has not scaled down yet
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "keda.json"), "w") as f:
        json.dump({"cold": cold, "warm": warm}, f, indent=1)


if __name__ == "__main__":
    main()
