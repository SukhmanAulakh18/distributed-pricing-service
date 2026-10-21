"""A live walkthrough of the running cluster, step by step. Each step says
what to look at and where. Interactive by default (Enter to continue);
--auto runs straight through, for recording.

Run: make demo   (after make cluster; make rancher for the Rancher views)
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bench"))
from books import book  # noqa: E402
from netting_set import netting_set  # noqa: E402

API, JAEGER, GRAFANA = "http://localhost:8080", "http://localhost:16686", "http://localhost:3001"
BOLD, DIM, END = "\033[1m", "\033[2m", "\033[0m"


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", "-n", "pricing", *args], capture_output=True, text=True).stdout.strip()


def step(n: int, title: str, auto: bool):
    print(f"\n{BOLD}── {n}. {title} {'─' * max(0, 60 - len(title))}{END}")
    if not auto:
        input(f"{DIM}   press Enter to run{END}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true")
    a = ap.parse_args()
    cid = open("deploy/rancher/.cluster-id").read().strip() if os.path.exists("deploy/rancher/.cluster-id") else None
    print(f"""{BOLD}Distributed pricing service: live demo{END}
   API docs      {API}/docs
   Grafana       {GRAFANA}/d/pricing-slo   (also /d/pricing and /d/pricing-cluster)
   Jaeger        {JAEGER}
   Alertmanager  http://localhost:9093
   Prometheus    http://localhost:9091/alerts""" + (f"\n   Rancher       https://localhost:8443/dashboard/c/{cid}/explorer" if cid else ""))

    step(1, "The cluster: 4 nodes, 3 zones", a.auto)
    print(subprocess.run(["kubectl", "get", "nodes", "-L", "topology.kubernetes.io/zone"], capture_output=True, text=True).stdout)
    print(kubectl("get", "pods", "-o", "wide").split("\n")[0])
    for line in kubectl("get", "pods", "-o", "wide").splitlines()[1:]:
        f = line.split()
        print(f"   {f[0]:42s} {f[2]:10s} {f[6]}")

    step(2, "One option: closed form vs Monte Carlo (C++ via pybind11)", a.auto)
    product = {"type": "european", "spot": 100, "strike": 105, "rate": 0.03, "vol": 0.2, "maturity": 1}
    for method in ("analytic", "mc"):
        r = httpx.post(f"{API}/price", json={"instrument_id": "DEMO", "market_data_version": f"md-{uuid.uuid4().hex[:6]}",
                                             "product": {**product, "method": method}, "simulation": {"n_paths": 1_000_000}}, timeout=30)
        d = r.json()
        print(f"   {method:9s} price {d['price']:.4f} ± {d['std_err']:.4f}   delta {d['delta']:.4f}   "
              f"served by {r.headers['x-served-by']}   trace {JAEGER}/trace/{r.headers.get('x-trace-id', '-')}")

    step(3, "A 10,000-trade book across the intraday worker pool", a.auto)
    t0 = time.time()
    r = httpx.post(f"{API}/portfolio/price", json={"book_id": "DEMO", "market_data_version": f"md-{uuid.uuid4().hex[:6]}",
                                                   "trades": book(10_000), "simulation": {"n_paths": 20_000}}, timeout=300)
    d = r.json()
    print(f"   {d['n_trades']:,} trades in {d['tasks']} tasks on {len(d['workers_used'])} workers, {time.time() - t0:.2f} s, "
          f"failed {d['n_failed']}\n   PV {d['pv']:,.0f} ± {d['pv_std_err']:,.0f}   "
          f"delta by underlying {json.dumps({k: round(v) for k, v in d['delta_by_underlying'].items()})}")
    print(f"   one trace across every worker: {JAEGER}/trace/{r.headers.get('x-trace-id', '-')}")

    step(4, "A batch exposure run: KEDA scales the batch pool up from zero", a.auto)
    print(f"   batch workers before: {kubectl('get', 'deploy', 'pricer-worker-batch', '-o', 'jsonpath={.status.readyReplicas}') or 0}")
    url = httpx.post(f"{API}/risk/exposure", json=netting_set(150, 200_000), timeout=30).json()["status_url"]
    seen = set()
    while (res := httpx.get(f"{API}{url}", timeout=30).json())["status"] == "running":
        seen.add(kubectl("get", "deploy", "pricer-worker-batch", "-o", "jsonpath={.status.readyReplicas}") or "0")
        time.sleep(2)
    print(f"   batch workers while running: {sorted(seen, key=int)}   ({res['n_scenarios']:,} scenarios, {res['elapsed_ms'] / 1e3:.1f} s)")
    print(f"   EPE {res['epe']:,.0f}   peak PFE95 {res['peak_pfe']:,.0f}   CVA {res['cva']:,.2f}")

    step(5, "Kill an API pod and a worker while a book is pricing", a.auto)
    api_pod = kubectl("get", "pods", "-l", "app=pricer-api", "-o", "jsonpath={.items[0].metadata.name}")
    worker = kubectl("get", "pods", "-l", "pool=intraday", "-o", "jsonpath={.items[0].metadata.name}")
    proc = subprocess.Popen([sys.executable, "-c", f"""
import httpx, json, sys, uuid; sys.path.insert(0, 'bench'); from books import book
r = httpx.post('{API}/portfolio/price', json={{'book_id': 'DEMO', 'market_data_version': 'md-'+uuid.uuid4().hex[:6], 'trades': book(10_000), 'simulation': {{'n_paths': 50_000}}}}, timeout=300).json()
print(json.dumps({{k: r[k] for k in ('n_trades', 'n_failed', 'elapsed_ms')}}))"""], stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    kubectl("delete", "pod", api_pod, worker, "--grace-period=0", "--force", "--wait=false")
    print(f"   killed {api_pod} and {worker} mid-book")
    print(f"   book result: {proc.communicate()[0].strip()}")

    step(6, "Triage: what is wrong, and who owns it", a.auto)
    time.sleep(5)
    subprocess.run([sys.executable, "-m", "tools.triage", "--api", API, "--namespace", "pricing"])
    print(f"\n{BOLD}Done.{END} Grafana SLOs: {GRAFANA}/d/pricing-slo   Rancher: https://localhost:8443")


if __name__ == "__main__":
    main()
