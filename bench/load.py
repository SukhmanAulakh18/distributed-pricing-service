"""Closed-loop HTTP load generator for /price.

Modes:
  latency  - N sequential requests; p50/p95/p99. --unique makes every request
             a cache miss; without it, all but the first are hits.
  steady   - --duration seconds at --concurrency, recording each request's
             time, status and latency. failure_test.sh kills pods while this
             runs; reports error rate, the window clients saw errors in, and
             the worst one-second latency.

Examples:
  python bench/load.py latency --url http://localhost:8000 -n 500 --unique
  python bench/load.py steady --url http://localhost:8080 --duration 60 --concurrency 16
"""
import argparse
import json
import threading
import time
import uuid

import httpx


def body(instrument: str, paths: int) -> dict:
    # A distinct strike per instrument makes each one its own contract (a cache miss).
    strike = 105.0 if instrument == "WARM" else 80 + int(instrument, 16) % 4_000_000 / 100_000
    return {"instrument_id": instrument, "market_data_version": "md-load",
            "product": {"type": "european", "spot": 100, "strike": strike, "rate": 0.03, "vol": 0.2, "maturity": 1.0},
            "simulation": {"n_paths": paths}}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def latency(args):
    lat, cached = [], 0
    with httpx.Client(base_url=args.url, timeout=30) as c:
        if not args.unique:
            c.post("/price", json=body("WARM", args.paths))
        for _ in range(args.n):
            b = body(uuid.uuid4().hex[:12] if args.unique else "WARM", args.paths)
            t0 = time.perf_counter()
            r = c.post("/price", json=b)
            lat.append((time.perf_counter() - t0) * 1e3)
            r.raise_for_status()
            cached += r.json()["cached"]
    print(f"n={args.n} paths={args.paths:,} unique={args.unique} cached={cached}/{args.n}")
    print(f"p50={pct(lat, 50):.2f} ms  p95={pct(lat, 95):.2f} ms  p99={pct(lat, 99):.2f} ms")


def steady(args):
    events = []  # (t_offset, ok, latency_ms)
    lock = threading.Lock()
    start = time.monotonic()
    stop = start + args.duration

    def worker(wid):
        with httpx.Client(base_url=args.url, timeout=args.timeout) as c:
            i = 0
            while time.monotonic() < stop:
                # Mostly unique requests so pods compute, some repeats for the cache.
                iid = f"{wid:x}{i % 5:x}" if i % 4 == 0 else uuid.uuid4().hex[:12]
                t0 = time.monotonic()
                try:
                    ok = c.post("/price", json=body(iid, args.paths)).status_code == 200
                except httpx.HTTPError:
                    ok = False
                with lock:
                    events.append((t0 - start, ok, (time.monotonic() - t0) * 1e3))
                i += 1

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(args.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    errors = [t for t, ok, _ in events if not ok]
    lat = [ms for _, ok, ms in events if ok]
    by_second: dict[int, list] = {}
    for t, _, ms in events:
        by_second.setdefault(int(t), []).append(ms)
    worst = max(by_second, key=lambda k: pct(by_second[k], 99))
    summary = {
        "requests": len(events), "errors": len(errors),
        "error_rate_pct": round(100 * len(errors) / max(len(events), 1), 3),
        "throughput_rps": round(len(events) / args.duration, 1),
        "p50_ms": round(pct(lat, 50), 2), "p99_ms": round(pct(lat, 99), 2),
        "worst_second_p99_ms": round(pct(by_second[worst], 99), 2), "worst_second_rps": len(by_second[worst]),
        "error_window_s": round(errors[-1] - errors[0], 2) if errors else 0.0,
    }
    if args.kill_at is not None:
        summary["recovery_after_kill_s"] = round(errors[-1] - args.kill_at, 2) if errors else 0.0
    print(json.dumps(summary, indent=2))
    if args.out:
        with open(args.out, "w") as f:
            json.dump({"summary": summary, "events": events}, f)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)
    lp = sub.add_parser("latency")
    lp.add_argument("--url", default="http://localhost:8000")
    lp.add_argument("-n", type=int, default=500)
    lp.add_argument("--paths", type=int, default=200_000)
    lp.add_argument("--unique", action="store_true")
    sp = sub.add_parser("steady")
    sp.add_argument("--url", default="http://localhost:8080")
    sp.add_argument("--duration", type=float, default=60)
    sp.add_argument("--concurrency", type=int, default=16)
    sp.add_argument("--paths", type=int, default=200_000)
    sp.add_argument("--timeout", type=float, default=5)
    sp.add_argument("--kill-at", type=float, default=None)
    sp.add_argument("--out")
    args = p.parse_args()
    latency(args) if args.mode == "latency" else steady(args)


if __name__ == "__main__":
    main()
