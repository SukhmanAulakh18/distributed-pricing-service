"""Kill portfolio workers mid-job and check that no trade is lost.

Submits a portfolio, SIGKILLs (or SIGTERMs) K worker containers part-way
through, and reports whether the job still completed, how many trades failed,
and how much the kill added to wall time versus an undisturbed baseline.

Run against the compose stack:
  python bench/worker_failure.py --kill 1 --signal KILL
  python bench/worker_failure.py --kill 1 --signal TERM
"""
import argparse
import os
import subprocess
import sys
import threading
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from books import book  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMPOSE = ["docker", "compose", "-f", os.path.join(ROOT, "deploy", "docker-compose.yml")]


def run_job(url, trades, paths, out):
    body = {"book_id": "FAIL", "market_data_version": f"fail-{uuid.uuid4().hex[:8]}", "trades": book(trades),
            "simulation": {"n_paths": paths}, "task_size": 100, "timeout_s": 120}
    t0 = time.perf_counter()
    r = httpx.post(f"{url}/portfolio/price", json=body, timeout=180)
    out["elapsed"] = time.perf_counter() - t0
    out["status"] = r.status_code
    out["body"] = r.json()


def worker_containers():
    ids = subprocess.check_output(COMPOSE + ["ps", "-q", "worker"], text=True).split()
    return ids


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--trades", type=int, default=10_000)
    p.add_argument("--paths", type=int, default=100_000)
    p.add_argument("--kill", type=int, default=1)
    p.add_argument("--kill-after", type=float, default=1.0)
    p.add_argument("--signal", default="KILL", choices=["KILL", "TERM"])
    a = p.parse_args()

    base = {}
    run_job(a.url, a.trades, a.paths, base)
    print(f"baseline: {base['elapsed']:.2f}s, failed={base['body']['n_failed']}, "
          f"workers={len(base['body']['workers_used'])}")

    res = {}
    t = threading.Thread(target=run_job, args=(a.url, a.trades, a.paths, res))
    t.start()
    time.sleep(a.kill_after)
    victims = worker_containers()[: a.kill]
    subprocess.run(["docker", "kill", "-s", a.signal, *victims], check=True, capture_output=True)
    print(f"sent SIG{a.signal} to {len(victims)} worker(s) at t={a.kill_after}s")
    t.join()

    b = res["body"]
    print(f"with kill: {res['elapsed']:.2f}s, http={res['status']}, failed={b['n_failed']}/{b['n_trades']}, "
          f"added={res['elapsed'] - base['elapsed']:+.2f}s")
    print(f"tasks per worker: {b['workers_used']}")

    # Bring the killed workers back for the next run.
    subprocess.run(COMPOSE + ["up", "-d", "--no-recreate", "worker"], capture_output=True)


if __name__ == "__main__":
    main()
