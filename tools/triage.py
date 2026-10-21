"""First-pass intraday triage: collects the signals RUNBOOK.md starts from
and turns them into findings, each with an owner to route it to.

  python -m tools.triage --api http://localhost:8080 --namespace pricing

Exit code: 0 all clear, 1 warnings, 2 something is broken. That makes it
usable from a cron job or an alert hook as well as by hand.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import httpx

# Who owns what, as in RUNBOOK.md "Who owns what".
OWNERS = {
    "core": "Core engineering (pricing service)",
    "quant": "Quant analytics (pricer numerics)",
    "platform": "Platform (Kubernetes, Redis)",
    "database": "Database team (Cassandra)",
}

INTRADAY_LAG_WARN = 100  # queued intraday tasks before we call it a backlog
RECENT = timedelta(hours=1)  # restarts older than this are history, not an incident


def owner_of(pod_name: str) -> str:
    if pod_name.startswith("cassandra"):
        return OWNERS["database"]
    if pod_name.startswith("redis"):
        return OWNERS["platform"]
    return OWNERS["core"]


def recent(ts: str | None, now: datetime) -> bool:
    if not ts:
        return False
    return now - datetime.fromisoformat(ts.replace("Z", "+00:00")) < RECENT


@dataclass
class Finding:
    severity: str  # "critical" | "warning" | "info"
    title: str
    evidence: str
    owner: str
    runbook: str


def parse_metrics(text: str) -> dict[str, float]:
    """Prometheus text format -> {'name{labels}': value}. Labels are kept so
    callers can match on them; comments and histograms buckets are skipped."""
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or "_bucket{" in line:
            continue
        m = re.match(r"^([a-zA-Z_:][\w:]*(?:\{[^}]*\})?)\s+([-+0-9.eEinfNa]+)$", line.strip())
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def metric(metrics: dict[str, float], name: str, **labels) -> float:
    total = 0.0
    for key, value in metrics.items():
        if key == name or key.startswith(name + "{"):
            if all(f'{k}="{v}"' in key for k, v in labels.items()):
                total += value
    return total


def collect(api: str, namespace: str | None) -> dict:
    snap: dict = {"api": api}
    with httpx.Client(base_url=api, timeout=5) as c:
        for key, path in (("health", "/health/ready"), ("queue", "/queue"), ("jobs", "/jobs?limit=200")):
            try:
                r = c.get(path)
                snap[key] = r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}: {r.text[:80]}"}
            except httpx.HTTPError as e:
                snap[key] = {"error": str(e)}
        try:
            snap["metrics"] = parse_metrics(c.get("/metrics").text)
        except httpx.HTTPError as e:
            snap["metrics"] = {}
            snap["metrics_error"] = str(e)
    if namespace and shutil.which("kubectl"):
        out = subprocess.run(["kubectl", "-n", namespace, "get", "pods", "-o", "json"], capture_output=True, text=True)
        snap["pods"] = json.loads(out.stdout).get("items", []) if out.returncode == 0 else []
        out = subprocess.run(["kubectl", "-n", namespace, "get", "hpa", "-o", "json"], capture_output=True, text=True)
        snap["hpas"] = json.loads(out.stdout).get("items", []) if out.returncode == 0 else []
    return snap


def analyze(snap: dict, now: datetime | None = None) -> list[Finding]:
    now = now or datetime.now(timezone.utc)
    f: list[Finding] = []
    health, m = snap.get("health", {}), snap.get("metrics", {})

    if "error" in health:
        f.append(Finding("critical", "API not answering health checks", health["error"], OWNERS["core"], "§2"))
        return f  # nothing else is trustworthy until the API answers

    checks = health.get("checks", {})
    if checks.get("redis") == "down" or metric(m, "dependency_circuit_state", dependency="redis") >= 2:
        f.append(Finding("warning", "Redis unavailable: pricing uncached, books priced on the API pods",
                         f"redis={checks.get('redis')}, circuit={metric(m, 'dependency_circuit_state', dependency='redis'):.0f}",
                         OWNERS["platform"], "§3"))
    store_errors = metric(m, "price_store_errors_total")
    dropped = metric(m, "price_log_dropped_rows_total")
    if checks.get("cassandra") == "down" or metric(m, "dependency_circuit_state", dependency="cassandra") >= 2:
        f.append(Finding("warning", "Cassandra unavailable: price log has a gap, pricing unaffected",
                         f"cassandra={checks.get('cassandra')}, write errors={store_errors:.0f}, dropped rows={dropped:.0f}",
                         OWNERS["database"], "§4"))
    elif store_errors or dropped:
        f.append(Finding("warning", "Price-log writes failing or dropped",
                         f"write errors={store_errors:.0f}, dropped rows={dropped:.0f}", OWNERS["database"], "§4"))

    rejected = metric(m, "compute_rejected_total")
    if rejected:
        f.append(Finding("warning", "Admission control rejecting requests (503): API pods saturated",
                         f"{rejected:.0f} rejected since pod start", OWNERS["core"], "§1"))
    # Client-facing pricing routes only: /queue and /jobs answer 503 by design
    # while their dependency is down, and that is already reported above.
    errors_5xx = sum(v for k, v in m.items() if k.startswith("http_request_seconds_count") and 'status="5' in k
                     and any(f'route="{r}' in k for r in ("/price", "/portfolio", "/risk")))
    if errors_5xx:
        f.append(Finding("warning", "HTTP 5xx on pricing endpoints", f"{errors_5xx:.0f} since pod start",
                         OWNERS["core"], "§2"))

    streams = snap.get("queue", {}).get("streams", {})
    for name, b in streams.items():
        intraday = "intraday" in name or name.endswith(":tasks")
        if intraday and b.get("lag", 0) > INTRADAY_LAG_WARN:
            f.append(Finding("critical", f"Intraday backlog on {name}", f"lag={b['lag']}, pending={b['pending']}",
                             OWNERS["core"], "§5"))
        elif b.get("pending", 0) and b.get("lag", 0) == 0 and not intraday:
            f.append(Finding("info", f"Batch work in flight on {name}", f"pending={b['pending']}", OWNERS["core"], "§5"))

    jobs = snap.get("jobs", [])
    if isinstance(jobs, list):
        bad = [j for j in jobs if j.get("status") in ("failed", "partial")]
        if bad:
            detail = "; ".join(f"{j['kind']} {j['job_id'][:8]} {j['status']} ({j.get('n_failed', 0)} failed)" for j in bad[:5])
            poison = any("max deliveries" in (j.get("detail") or "") for j in bad)
            f.append(Finding("warning", f"{len(bad)} job(s) failed or partial today", detail,
                             OWNERS["quant"] if poison else OWNERS["core"], "§5"))

    for pod in snap.get("pods", []):
        name = pod["metadata"]["name"]
        for cs in pod.get("status", {}).get("containerStatuses", []):
            last = cs.get("lastState", {}).get("terminated") or {}
            if not recent(last.get("finishedAt"), now):
                continue  # no termination in the last hour: restartCount is just history
            if last.get("reason") == "OOMKilled":
                f.append(Finding("critical", f"{name} was OOM-killed", f"restarts={cs['restartCount']}",
                                 owner_of(name), "§2"))
            else:
                f.append(Finding("warning", f"{name} restarted in the last hour",
                                 f"restarts={cs['restartCount']}, last exit: {last.get('reason')} "
                                 f"(code {last.get('exitCode')}) at {last.get('finishedAt')}", owner_of(name), "§6"))
        for cs in pod.get("status", {}).get("containerStatuses", []):
            if not cs.get("ready") and pod["metadata"].get("deletionTimestamp") is None:
                f.append(Finding("warning", f"{name} not ready", json.dumps(cs.get("state", {}))[:120],
                                 owner_of(name), "§2"))
        if pod.get("status", {}).get("phase") == "Pending":
            f.append(Finding("warning", f"{name} Pending (unschedulable?)", "check node capacity and requests",
                             OWNERS["platform"], "§2"))
    for hpa in snap.get("hpas", []):
        spec, status = hpa["spec"], hpa.get("status", {})
        if status.get("currentReplicas", 0) >= spec["maxReplicas"]:
            f.append(Finding("warning", f"HPA {hpa['metadata']['name']} at maxReplicas",
                             f"{status.get('currentReplicas')}/{spec['maxReplicas']}", OWNERS["platform"], "§1"))
    return f


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--api", default="http://localhost:8080")
    p.add_argument("--namespace", help="Kubernetes namespace to inspect with kubectl")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    findings = analyze(collect(args.api, args.namespace))
    if args.json:
        print(json.dumps([asdict(x) for x in findings], indent=2))
    else:
        if not findings:
            print("All clear: API healthy, dependencies up, no backlog, no failed jobs today.")
        for x in sorted(findings, key=lambda x: ["critical", "warning", "info"].index(x.severity)):
            print(f"[{x.severity.upper():8s}] {x.title}\n           evidence: {x.evidence}\n"
                  f"           route to: {x.owner}  (RUNBOOK {x.runbook})")
    severities = {x.severity for x in findings}
    return 2 if "critical" in severities else 1 if "warning" in severities else 0


if __name__ == "__main__":
    sys.exit(main())
