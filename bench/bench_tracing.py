"""What tracing costs, and what one traced book looks like.

1. /price throughput with every request traced (OTEL_SAMPLE_RATIO=1.0)
   versus none (0.0), same load, on the kind cluster.
2. A 2,000-trade book's trace from Jaeger: spans, and how many pods it crossed.

Run (kind cluster up): python bench/bench_tracing.py
"""
import json
import os
import subprocess
import sys
import time
import types

import httpx

sys.path.insert(0, os.path.dirname(__file__))
import load  # noqa: E402
from books import book  # noqa: E402

API, JAEGER = "http://localhost:8080", "http://localhost:16686"
OUT = os.path.join(os.path.dirname(__file__), "results")


def sample_ratio(ratio: str):
    subprocess.run(["kubectl", "-n", "pricing", "set", "env", "deploy/pricer-api", f"OTEL_SAMPLE_RATIO={ratio}"],
                   check=True, capture_output=True)
    subprocess.run(["kubectl", "-n", "pricing", "rollout", "status", "deploy/pricer-api", "--timeout=300s"],
                   check=True, capture_output=True)
    time.sleep(20)


def throughput() -> float:
    args = types.SimpleNamespace(url=API, duration=45, concurrency=12, paths=50_000, timeout=5, kill_at=None,
                                 out=os.path.join(OUT, "tracing-load.json"))
    load.steady(args)
    return json.load(open(args.out))["summary"]["throughput_rps"]


def book_trace() -> dict:
    r = httpx.post(f"{API}/portfolio/price", json={"book_id": "TRACE", "market_data_version": f"md-{time.time()}",
                                                   "trades": book(2_000), "simulation": {"n_paths": 20_000},
                                                   "task_size": 50}, timeout=120)
    trace_id = r.headers["x-trace-id"]
    time.sleep(8)  # let the batch span processors flush
    data = httpx.get(f"{JAEGER}/api/v3/traces/{trace_id}", timeout=30).json()
    spans, pods, names = 0, set(), {}
    for rs in data["result"]["resourceSpans"]:
        attrs = {a["key"]: a["value"].get("stringValue") for a in rs["resource"]["attributes"]}
        pods.add(attrs.get("service.instance.id"))
        for ss in rs["scopeSpans"]:
            for s in ss["spans"]:
                spans += 1
                name = " ".join(dict.fromkeys(s["name"].split())) + (" (pipeline)" if " " in s["name"] else "")
                names[name] = names.get(name, 0) + 1
    return {"trace_id": trace_id, "trades": 2_000, "spans": spans, "pods": len(pods),
            "top_operations": dict(sorted(names.items(), key=lambda kv: -kv[1])[:8])}


def main():
    sample_ratio("0.0")
    off = throughput()
    sample_ratio("1.0")
    on = throughput()
    trace = book_trace()
    result = {"overhead": {"off_rps": off, "on_rps": on, "cost_pct": 100 * (off - on) / off}, "book_trace": trace}
    print(json.dumps(result, indent=1))
    with open(os.path.join(OUT, "tracing.json"), "w") as f:
        json.dump(result, f, indent=1)


if __name__ == "__main__":
    main()
