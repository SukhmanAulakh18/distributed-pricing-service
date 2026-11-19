"""Build docs/data/platform.json for the platform page (docs/platform.html).

Reads the measured result files in bench/results and keeps only the series the
page draws. Deterministic: sorted keys, rounded floats, no timestamps. Run with
--check to fail if the committed file is stale (tests/test_platform_data.py
does the same)."""
import argparse
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
RES = os.path.join(ROOT, "bench", "results")
OUT = os.path.join(ROOT, "docs", "data", "platform.json")

BIN_S = 5  # chaos availability line: one point per 5 s
WINDOW_S = 30  # look-ahead after a fault when scoring a recovery window


def load(name):
    with open(os.path.join(RES, name)) as f:
        return json.load(f)


def r(x, n=3):
    return round(x, n)


def p99(vals):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * 0.99))] if vals else 0.0


def chaos(c):
    prices, faults, s = c["prices"], c["faults"], c["summary"]
    nbins = int(s["minutes"] * 60 // BIN_S)
    bins = [[0, 0, []] for _ in range(nbins)]
    for t, ok, ms in prices:
        b = min(nbins - 1, int(t // BIN_S))
        bins[b][0] += 1
        bins[b][1] += 0 if ok else 1
        bins[b][2].append(ms)
    line = [[i * BIN_S, n, e, r(p99(v), 1)] for i, (n, e, v) in enumerate(bins)]
    fl = [{"t": r(f["t"], 1), "fault": f["fault"]} for f in faults]
    wins = []
    for f in faults:
        w = [p for p in prices if f["t"] <= p[0] < f["t"] + WINDOW_S]
        wins.append({"t": r(f["t"], 1), "fault": f["fault"], "requests": len(w),
                     "errors": sum(1 for p in w if not p[1]),
                     "p99_ms": r(p99([p[2] for p in w]), 1)})
    errs = [[r(t, 1), r(ms, 1)] for t, ok, ms in prices if not ok]
    keep = ("minutes", "faults", "price_requests", "price_errors", "availability_pct",
            "p50_ms", "p99_ms", "books", "books_complete", "books_retried",
            "trades_priced", "trades_failed", "exposure_runs", "exposure_complete",
            "exposure_identical", "alerts_fired")
    return {"summary": {k: s[k] for k in keep},
            "bin_s": BIN_S, "line": line, "faults": fl, "windows": wins, "errors": errs}


def build():
    core, exp, keda = load("core.json"), load("exposure.json"), load("keda.json")
    pf, pri, wb = load("portfolio.json"), load("priority.json"), load("write_behind.json")
    tr, siege, c3 = load("tracing.json"), load("siege.json"), load("chaos-run3.json")
    batch = {b["method"]: b["options_per_s"] for b in core["batch"]}
    prof = exp["profile"]
    ch = chaos(c3)
    cass = [w for w in ch["windows"] if w["fault"] == "cassandra node"]
    proof = {
        "bs_per_s": batch["bs_batch, 8 threads"],
        "availability_pct": c3["summary"]["availability_pct"],
        "identical_workers": [s["workers"] for s in exp["scaling"] if s["identical"]],
        "siege_passed": siege["passed"], "siege_total": siege["passed"] + siege["failed"],
        "trades_failed": c3["summary"]["trades_failed"],
        "trades_priced": c3["summary"]["trades_priced"],
        "cassandra_kill_requests": sum(w["requests"] for w in cass),
        "cassandra_kill_errors": sum(w["errors"] for w in cass),
    }
    return {
        "proof": proof,
        "env": core["env"],
        "gil": [{"gil": g["gil"], "threads": g["python_threads"], "per_s": r(g["calls_per_s"], 1)}
                for g in core["gil"]],
        "batch": [{"method": b["method"], "per_s": round(b["options_per_s"])} for b in core["batch"]],
        "threads": [{"threads": t["threads"], "ms": r(t["ms"], 1), "speedup": r(t["speedup"], 2)}
                    for t in core["threads"]],
        "convergence": [{"paths": c["paths"], "antithetic": c["antithetic"],
                         "std_err": r(c["std_err"], 5), "abs_err": r(c["abs_err"], 5)}
                        for c in core["convergence"]],
        "black_scholes": r(core["black_scholes"], 4),
        "variance_reduction": [{"setting": v["setting"], "std_err": r(v["std_err"], 5),
                                "factor": r(v["variance_reduction"], 1),
                                "paths": v["paths_to_match_plain"]}
                               for v in core["variance_reduction"]],
        "exposure": {
            "revaluations": exp["revaluations"],
            "scaling": [{"workers": s["workers"], "wall_s": r(s["wall_s"], 2),
                         "per_s": round(s["revals_per_s"]), "identical": s["identical"]}
                        for s in exp["scaling"]],
            "grid": [r(g, 3) for g in prof["grid"]],
            "pfe": [r(x, 1) for x in prof["pfe"]],
            "ee": [r(x, 1) for x in prof["ee"]],
            "peak_pfe": r(prof["peak_pfe"], 1), "cva": r(prof["cva"], 3),
            "epe": r(prof["epe"], 1), "n_scenarios": prof["n_scenarios"],
        },
        "portfolio": {
            "scaling": [{"book": s["book"], "workers": s["workers"], "per_s": round(s["trades_per_s"])}
                        for s in pf["scaling"]],
            "task_size": [{"task_size": s["task_size"], "per_s": round(s["trades_per_s"]),
                           "tasks": s["tasks"]} for s in pf["task_size"]],
        },
        "priority": [{"mode": p["mode"], "idle_s": r(p["intraday_idle_s"], 2),
                      "during_batch_s": r(p["intraday_during_batch_s"], 2),
                      "batch_s": r(p["batch_s"], 2)} for p in pri],
        "keda": {k: {"first_ready_s": r(v["first_ready_s"], 1), "total_s": r(v["total_s"], 1),
                     "timeline": v["timeline"]} for k, v in keda.items()},
        "write_behind": wb,
        "tracing": {"overhead": {k: r(v, 1) for k, v in tr["overhead"].items()},
                    "spans": tr["book_trace"]["spans"], "pods": tr["book_trace"]["pods"],
                    "trades": tr["book_trace"]["trades"]},
        "chaos": ch,
        "siege": [{"scenario": s["scenario"], "fault": s["fault"], "recovered_s": s["recovered_s"],
                   "passed": s["passed"], "routed_to": s["routed_to"], "alert": s["alert"],
                   "detected_by": s["detected_by"]} for s in siege["scenarios"]],
    }


def render():
    return json.dumps(build(), sort_keys=True, separators=(",", ":")) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    out = render()
    if a.check:
        with open(OUT) as f:
            if f.read() != out:
                sys.exit("docs/data/platform.json is stale; run scripts/build_platform_data.py")
        return
    with open(OUT, "w") as f:
        f.write(out)
    print(f"wrote {OUT} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
