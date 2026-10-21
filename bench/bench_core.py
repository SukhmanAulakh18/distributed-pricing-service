"""C++ core benchmarks: accuracy, variance reduction, threads, the GIL, and
batch versus per-call pricing. Writes bench/results/core.json and prints
Markdown tables.

Run: python bench/bench_core.py
"""
import json
import math
import os
import platform
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pricer_core as pc

ARGS = dict(spot=100, strike=105, rate=0.03, vol=0.2, maturity=1.0)
BS = pc.black_scholes(100, 105, 0.03, 0.0, 0.2, 1.0, True).price
OUT = os.path.join(os.path.dirname(__file__), "results")


def best_time(fn, repeats=9):
    """Best of N. On a laptop the noise is interference from other processes
    (it only ever adds time), so the minimum is the cleanest estimate of what
    the code itself costs."""
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return min(times)


def convergence():
    rows = []
    for n in (10_000, 100_000, 1_000_000, 10_000_000):
        for anti in (False, True):
            r = pc.price_european_mc(**ARGS, n_paths=n, antithetic=anti, seed=42, threads=8).price
            rows.append({"paths": n, "antithetic": anti, "price": r.value, "std_err": r.std_err,
                         "abs_err": abs(r.value - BS)})
    return rows


def variance_reduction():
    n = 200_000
    plain = pc.price_asian_mc(**ARGS, n_fixings=12, n_paths=n, antithetic=False, control_variate=False, seed=1, threads=8)
    anti = pc.price_asian_mc(**ARGS, n_fixings=12, n_paths=n, antithetic=True, control_variate=False, seed=1, threads=8)
    cv = pc.price_asian_mc(**ARGS, n_fixings=12, n_paths=n, antithetic=True, control_variate=True, seed=1, threads=8)
    rows = []
    for name, r in (("plain", plain), ("antithetic", anti), ("antithetic + control variate", cv)):
        ratio = (plain.price.std_err / r.price.std_err) ** 2
        rows.append({"setting": name, "price": r.price.value, "std_err": r.price.std_err,
                     "variance_reduction": ratio, "paths_to_match_plain": int(n / ratio)})
    return rows


def threads():
    rows, base = [], None
    for t in (1, 2, 4, 8, 10):
        dt = best_time(lambda: pc.price_european_mc(**ARGS, n_paths=10_000_000, seed=42, threads=t))
        base = base or dt
        rows.append({"threads": t, "ms": dt * 1e3, "speedup": base / dt})
    return rows


def gil():
    """N Python threads each price on one C++ thread: with the GIL released
    they run in parallel; holding it serialises them."""
    rows = []
    calls = 16
    for fn, name in ((pc.price_european_mc, "released"), (pc._price_european_mc_holding_gil, "held")):
        for t in (1, 2, 4, 8):
            def run():
                with ThreadPoolExecutor(t) as ex:
                    list(ex.map(lambda i: fn(**ARGS, n_paths=1_000_000, seed=i, threads=1), range(calls)))
            dt = best_time(run, repeats=3)
            rows.append({"gil": name, "python_threads": t, "calls_per_s": calls / dt})
    return rows


def batch():
    """1M analytic prices: a Python loop, per-call bindings, and one batched
    call (zero-copy NumPy in and out, GIL released, C++ threads)."""
    rng = np.random.default_rng(0)
    n = 1_000_000
    s, k = np.full(n, 100.0), rng.uniform(70, 130, n)
    v, t, q = rng.uniform(0.1, 0.6, n), rng.uniform(0.1, 3, n), np.zeros(n)
    c = rng.random(n) < 0.5
    sample = 50_000

    def py_bs(i):
        d1 = (math.log(s[i] / k[i]) + (0.03 + 0.5 * v[i] ** 2) * t[i]) / (v[i] * math.sqrt(t[i]))
        d2 = d1 - v[i] * math.sqrt(t[i])
        nd = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731
        return s[i] * nd(d1) - k[i] * math.exp(-0.03 * t[i]) * nd(d2) if c[i] else \
            k[i] * math.exp(-0.03 * t[i]) * nd(-d2) - s[i] * nd(-d1)

    sl, kl, vl, tl, cl = s.tolist(), k.tolist(), v.tolist(), t.tolist(), c.tolist()
    rows = [
        {"method": "pure Python loop", "options_per_s": sample / best_time(lambda: [py_bs(i) for i in range(sample)], 3)},
        {"method": "per-call C++ binding", "options_per_s": sample / best_time(
            lambda: [pc.black_scholes(sl[i], kl[i], 0.03, 0.0, vl[i], tl[i], cl[i]) for i in range(sample)], 3)},
    ]
    for th in (1, 8):
        rows.append({"method": f"bs_batch, {th} thread{'s' * (th > 1)}",
                     "options_per_s": n / best_time(lambda: pc.bs_batch(s, k, 0.03, q, v, t, c, threads=th))})
    return rows


def main():
    env = {"machine": platform.machine(), "cores": os.cpu_count(), "platform": platform.platform(),
           "python": platform.python_version()}
    results = {"env": env, "black_scholes": BS, "convergence": convergence(),
               "variance_reduction": variance_reduction(), "threads": threads(), "gil": gil(), "batch": batch()}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "core.json"), "w") as f:
        json.dump(results, f, indent=1)

    print(f"Environment: {env}\nEuropean call S=100 K=105 r=3% vol=20% T=1y, Black-Scholes {BS:.6f}\n")
    print("| Paths | Antithetic | Price | Std. error | Abs. error |\n| --- | --- | --- | --- | --- |")
    for r in results["convergence"]:
        print(f"| {r['paths']:,} | {'yes' if r['antithetic'] else 'no'} | {r['price']:.4f} | {r['std_err']:.4f} | {r['abs_err']:.4f} |")
    print("\n| Asian (12 fixings, 200k paths) | Std. error | Variance reduction | Paths to match plain's error |\n| --- | --- | --- | --- |")
    for r in results["variance_reduction"]:
        print(f"| {r['setting']} | {r['std_err']:.5f} | {r['variance_reduction']:.1f}x | {r['paths_to_match_plain']:,} |")
    print("\n| C++ threads (10M paths) | Time | Speedup |\n| --- | --- | --- |")
    for r in results["threads"]:
        print(f"| {r['threads']} | {r['ms']:.0f} ms | {r['speedup']:.2f}x |")
    print("\n| GIL | Python threads | Pricings/s |\n| --- | --- | --- |")
    for r in results["gil"]:
        print(f"| {r['gil']} | {r['python_threads']} | {r['calls_per_s']:.1f} |")
    print("\n| Analytic pricing | Options/s |\n| --- | --- |")
    for r in results["batch"]:
        print(f"| {r['method']} | {r['options_per_s']:,.0f} |")


if __name__ == "__main__":
    main()
