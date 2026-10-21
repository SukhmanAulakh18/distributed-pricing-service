"""Chaos run on the kind cluster: realistic load while components fail.

For --minutes, three workloads run at once:
  * single prices: --clients closed-loop clients on /price (fresh contracts)
  * intraday books: a 2,000-trade book every 15 s
  * batch exposure: the same 100,000-scenario exposure request every 150 s,
    whose result must come back bit-identical every time
while a fixed schedule of faults hits the system roughly every 45 s:
SIGKILLs of API pods, intraday and batch workers, Redis and a Cassandra
node, and one whole Kubernetes node stopped for two minutes (long enough for Kubernetes to declare it NotReady,
fire the alert, evict its pods and reschedule them).

Reports availability, latency, books and trades lost, exposure runs and
whether they matched, which alerts fired (from Prometheus), and writes
bench/results/chaos.json for the chart.

Run: python bench/chaos.py --minutes 10
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from books import book  # noqa: E402
from load import body as price_body  # noqa: E402
from netting_set import netting_set  # noqa: E402

NS = "pricing"
OUT = os.path.join(os.path.dirname(__file__), "results")


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True).stdout.strip()


def pods(selector: str) -> list[tuple[str, str]]:
    out = sh("kubectl", "-n", NS, "get", "pods", "-l", selector, "--field-selector=status.phase=Running",
             "-o", "jsonpath={range .items[*]}{.metadata.name} {.spec.nodeName}{\"\\n\"}{end}")
    return [tuple(line.split()) for line in out.splitlines() if line.strip()]


def crash(selector: str) -> str:
    """SIGKILL a pod's main process on its node: a crash, not a shutdown."""
    found = pods(selector)
    if not found:
        return f"no running pod for {selector}"
    pod, node = found[0]
    cid = sh("docker", "exec", node, "crictl", "ps", "-q", "--label", f"io.kubernetes.pod.name={pod}").split("\n")[0]
    pid = sh("docker", "exec", node, "crictl", "inspect", "-o", "go-template", "--template", "{{.info.pid}}", cid)
    sh("docker", "exec", node, "kill", "-9", pid)
    return f"SIGKILL {pod} on {node}"


def stop_node(seconds: int) -> str:
    """Stop a whole Kubernetes node (its container) and start it again later.
    Picks a node without Redis, so the run shows rescheduling and Cassandra
    quorum rather than a Redis outage (that is a separate fault)."""
    redis_node = (pods("app=redis") or [("", "")])[0][1]
    nodes = [n for n in sh("kubectl", "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}").split()
             if "control-plane" not in n and n != redis_node]
    node = nodes[0]
    sh("docker", "stop", node)
    threading.Timer(seconds, lambda: sh("docker", "start", node)).start()
    return f"stopped node {node} for {seconds}s"


FAULTS = [  # (seconds into the run, name, action)
    (45, "api pod", lambda: crash("app=pricer-api")),
    (90, "intraday worker", lambda: crash("app=pricer-worker,pool=intraday")),
    (135, "cassandra node", lambda: crash("app=cassandra")),
    (180, "batch worker", lambda: crash("app=pricer-worker,pool=batch")),
    (225, "redis", lambda: crash("app=redis")),
    (270, "api pod", lambda: crash("app=pricer-api")),
    (315, "whole k8s node", lambda: stop_node(120)),
    (420, "intraday worker", lambda: crash("app=pricer-worker,pool=intraday")),
    (465, "cassandra node", lambda: crash("app=cassandra")),
    (510, "api pod", lambda: crash("app=pricer-api")),
    (555, "redis", lambda: crash("app=redis")),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--prometheus", default="http://localhost:9091")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--clients", type=int, default=8)
    ap.add_argument("--paths", type=int, default=50_000)
    ap.add_argument("--book-retries", type=int, default=1)
    a = ap.parse_args()

    start = time.monotonic()
    t0_wall = time.time()
    stop_at = start + a.minutes * 60
    now = lambda: time.monotonic() - start  # noqa: E731
    lock = threading.Lock()
    prices, books, exposures, faults = [], [], [], []

    def price_client(cid):
        with httpx.Client(base_url=a.api, timeout=5) as c:
            while time.monotonic() < stop_at:
                t = now()
                try:
                    ok = c.post("/price", json=price_body(uuid.uuid4().hex[:12], a.paths)).status_code == 200
                except httpx.HTTPError:
                    ok = False
                with lock:
                    prices.append((t, ok, (now() - t) * 1e3))

    def book_loop():
        trades = book(2_000, seed=42)
        while time.monotonic() < stop_at - 20:
            t = now()
            req = {"book_id": "CHAOS", "market_data_version": f"md-{uuid.uuid4().hex[:8]}", "trades": trades,
                   "simulation": {"n_paths": 20_000}, "task_size": 50, "timeout_s": 120}
            rec = {"t": t, "attempts": 0}
            while rec["attempts"] <= a.book_retries:
                rec["attempts"] += 1
                try:
                    r = httpx.post(f"{a.api}/portfolio/price", json=req, timeout=150)
                    d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    rec.update(s=now() - t, status=r.status_code, failed=d.get("n_failed"), mode=d.get("mode"))
                    break
                except httpx.HTTPError as e:
                    # A book is idempotent (same input, same prices), so a
                    # client may retry a connection-level failure.
                    rec.update(s=now() - t, status=0, failed=None, error=str(e))
                    time.sleep(2)
            with lock:
                books.append(rec)
            time.sleep(max(0.0, 15 - (now() - t)))

    def exposure_loop():
        req = netting_set(n_trades=100, n_scenarios=100_000, block_size=5_000)
        while time.monotonic() < stop_at - 60:
            t = now()
            rec = {"t": t}
            try:
                try:
                    url = httpx.post(f"{a.api}/risk/exposure", json=req, timeout=30).json()["status_url"]
                except httpx.TransportError:  # resubmitting is safe: same request, same result
                    time.sleep(2)
                    url = httpx.post(f"{a.api}/risk/exposure", json=req, timeout=30).json()["status_url"]
                while time.monotonic() < stop_at + 120:
                    try:
                        res = httpx.get(f"{a.api}{url}", timeout=10).json()
                    except (httpx.HTTPError, ValueError):
                        time.sleep(1)
                        continue
                    if res.get("status") in ("complete", "failed"):
                        rec.update(status=res["status"], s=now() - t, cva=res.get("cva"), pfe=res.get("pfe"))
                        break
                    time.sleep(1)
            except (httpx.HTTPError, KeyError, ValueError) as e:
                rec.update(status="submit-error", error=str(e))
            with lock:
                exposures.append(rec)
            time.sleep(max(0.0, 150 - (now() - t)))

    def fault_loop():
        for at, name, action in FAULTS:
            if at > a.minutes * 60 - 30:
                break
            time.sleep(max(0.0, at - now()))
            detail = action()
            faults.append({"t": now(), "fault": name, "detail": detail})
            print(f"[{now():6.1f}s] {name}: {detail}", flush=True)

    threads = [threading.Thread(target=price_client, args=(i,)) for i in range(a.clients)]
    threads += [threading.Thread(target=f) for f in (book_loop, exposure_loop, fault_loop)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = [p for p in prices if p[1]]
    lat = sorted(p[2] for p in ok)
    errors = len(prices) - len(ok)
    done = [e for e in exposures if e.get("status") == "complete"]
    ref = done[0] if done else None
    identical = all(e["pfe"] == ref["pfe"] and e["cva"] == ref["cva"] for e in done) if done else None
    alerts = []
    try:
        q = {"query": 'max_over_time(ALERTS{alertstate="firing"}[' + f"{int(a.minutes * 60) + 120}s])"}
        res = httpx.get(f"{a.prometheus}/api/v1/query", params=q, timeout=10).json()["data"]["result"]
        alerts = sorted({(r["metric"]["alertname"], r["metric"].get("owner", "")) for r in res})
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    summary = {
        "minutes": a.minutes, "faults": len(faults),
        "price_requests": len(prices), "price_errors": errors,
        "availability_pct": round(100 * len(ok) / max(len(prices), 1), 3),
        "p50_ms": round(lat[len(lat) // 2], 1) if lat else None,
        "p99_ms": round(lat[int(len(lat) * 0.99)], 1) if lat else None,
        "books": len(books), "books_complete": sum(1 for b in books if b["status"] == 200 and b["failed"] == 0),
        "books_partial": sum(1 for b in books if b["status"] == 206),
        "books_retried": sum(1 for b in books if b.get("attempts", 1) > 1),
        "trades_priced": sum(2_000 for b in books if b["status"] in (200, 206)),
        "trades_failed": sum(b["failed"] or 0 for b in books if b["status"] in (200, 206)),
        "exposure_runs": len(exposures), "exposure_complete": len(done), "exposure_identical": identical,
        "alerts_fired": [f"{n} -> {o}" for n, o in alerts],
    }
    print(json.dumps(summary, indent=2))
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "chaos.json"), "w") as f:
        json.dump({"summary": summary, "started_at": t0_wall, "faults": faults, "books": books,
                   "exposures": [{k: v for k, v in e.items() if k != "pfe"} for e in exposures],
                   "prices": prices}, f)


if __name__ == "__main__":
    main()
