"""Guarded remediation runner: triage findings in, allowlisted actions out,
each one proven by replaying the matching siege test, all of it in a
hash-chained ledger. No LLM, no cluster access, no commands from input.

  python -m tools.remediate --demo redis
  python -m tools.remediate --findings snapshot.json --approve --by alice
  python -m tools.remediate --verify-ledger

See docs/REMEDIATION.md.
"""
import argparse
import getpass
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from tools.triage import Finding, analyze

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "var" / "remediation-ledger.jsonl"
GENESIS = "0" * 64
EVIDENCE_TIMEOUT_S = 120

# The whole policy. A finding kind with no entry here is refused, and the
# evidence argv is built only from `evidence_test` below, never from input.
ALLOWLIST = {
    "redis_down": {"action": "replay_incident", "mutates": False, "runbook_section": "§3",
                   "evidence_test": "tests/siege/test_redis_outage.py::test_redis_outage_mid_book"},
    "failed_jobs": {"action": "collect_diagnostics", "mutates": False, "runbook_section": "§5",
                    "evidence_test": "tests/siege/test_poison.py::test_poisoned_messages_do_not_stop_the_book"},
    "pod_restart": {"action": "replay_incident", "mutates": False, "runbook_section": "§6",
                    "evidence_test": "tests/siege/test_worker_death.py::test_worker_killed_mid_task"},
    "intraday_backlog": {"action": "advise_scale_out", "mutates": False, "runbook_section": "§5",
                         "evidence_test": "tests/siege/test_exposure_kill.py::test_exposure_run_with_a_worker_killed"},
    "http_5xx": {"action": "resubmit_with_same_idempotency_key", "mutates": True, "runbook_section": "§2",
                 "evidence_test": "tests/siege/test_client_reset.py::test_client_reset_then_retry_with_same_key"},
}

# Triage findings carry a title, not a kind; map the titles it can emit.
# Anything unrecognised (Cassandra, HPA, ...) gets kind None and is refused.
_KIND_BY_TITLE = (("Redis unavailable", "redis_down"), ("failed or partial", "failed_jobs"),
                  ("restarted in the last hour", "pod_restart"), ("Intraday backlog", "intraday_backlog"),
                  ("HTTP 5xx on pricing", "http_5xx"))


def kind_of(f: Finding) -> str | None:
    return next((k for needle, k in _KIND_BY_TITLE if needle in f.title), None)


def _execute(action: str, f: Finding) -> str:
    # Deliberately local and inert: nothing here touches a cluster or the
    # network. The evidence test, not this return value, decides the outcome.
    return {"collect_diagnostics": f"diagnostics from finding: {f.evidence}",
            "replay_incident": "replaying the matching siege scenario as evidence",
            "advise_scale_out": f"advice: scale out the workers, RUNBOOK {f.runbook}",
            "resubmit_with_same_idempotency_key": "retry with the original Idempotency-Key (server dedupes)",
            }[action]


def run_evidence(test: str, timeout: int = EVIDENCE_TIMEOUT_S) -> dict:
    """The only subprocess. `test` always comes from ALLOWLIST."""
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
                           cwd=ROOT, capture_output=True, timeout=timeout)
        passed = p.returncode == 0
    except subprocess.TimeoutExpired:
        passed = False
    return {"test": test, "passed": passed, "seconds": round(time.monotonic() - t0, 2)}


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def append(path: Path, entry: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    entry["prev_hash"] = json.loads(lines[-1])["hash"] if lines else GENESIS
    entry["hash"] = _digest(entry)
    with path.open("a") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def verify_ledger(path: Path) -> int | None:
    """Line number (1-based) of the first broken entry, or None if intact.
    Catches edits, deletions and reordering; a truncated tail is not detectable
    without an external anchor of the last hash."""
    prev = GENESIS
    if not path.exists():
        return None
    for n, line in enumerate(path.read_text().splitlines(), 1):
        try:
            e = json.loads(line)
            ok = e["prev_hash"] == prev and e["hash"] == _digest(e)
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            return n
        prev = e["hash"]
    return None


def process(f: Finding, *, ledger: Path, who: str, approve: bool = False, by: str | None = None,
            evidence_fn=None) -> dict:
    evidence_fn = evidence_fn or run_evidence
    policy = ALLOWLIST.get(kind_of(f))
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "who": who, "finding": asdict(f), "owner": f.owner,
             "runbook_section": policy["runbook_section"] if policy else f.runbook,
             "action": policy["action"] if policy else None, "mutates": policy["mutates"] if policy else None,
             "approved_by": None, "evidence": None}
    if not policy:
        entry["outcome"] = "refused"  # not on the allowlist: do nothing
    elif policy["mutates"] and not approve:
        entry["outcome"] = "proposed"  # dry-run default: awaiting approval
    elif policy["mutates"] and (not by or by == who):
        # Segregation of duties is enforced here, not only by the CLI, so no
        # caller can approve its own mutating action.
        entry["outcome"] = "refused"
        entry["detail"] = "a mutating action needs an approver other than the operator"
    else:
        if policy["mutates"]:
            entry["approved_by"] = by
        entry["detail"] = _execute(policy["action"], f)
        entry["evidence"] = evidence_fn(policy["evidence_test"])
        entry["outcome"] = "verified" if entry["evidence"]["passed"] else "unverified"
    return append(ledger, entry)


def _demo_snapshot(scenario: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    ok = {"health": {"checks": {"redis": "up", "cassandra": "up"}}, "metrics": {}}
    if scenario == "redis":
        return {**ok, "health": {"checks": {"redis": "down", "cassandra": "up"}}}
    if scenario == "poison":
        return {**ok, "jobs": [{"kind": "portfolio", "job_id": "9f3a1c2e77", "status": "partial", "n_failed": 2,
                                "detail": "task hit max deliveries"}]}
    if scenario == "worker":
        pod = {"metadata": {"name": "pricing-worker-7d9f"}, "status": {"phase": "Running", "containerStatuses": [
            {"ready": True, "restartCount": 1, "lastState": {"terminated": {
                "reason": "Error", "exitCode": 137, "finishedAt": now}}}]}}
        return {**ok, "pods": [pod]}
    if scenario == "backlog":
        return {**ok, "queue": {"streams": {"pricing:intraday:tasks": {"lag": 500, "pending": 3}}}}
    if scenario == "reset":
        return {**ok, "metrics": {'http_request_seconds_count{route="/price",status="502"}': 4.0}}
    raise SystemExit(f"unknown demo scenario {scenario!r}; one of: redis poison worker backlog reset")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = p.add_mutually_exclusive_group()
    src.add_argument("--findings", type=Path, help="triage snapshot JSON, analysed by tools.triage")
    src.add_argument("--demo", help="redis | poison | worker | backlog | reset")
    src.add_argument("--verify-ledger", action="store_true")
    p.add_argument("--ledger", type=Path, default=LEDGER)
    p.add_argument("--approve", action="store_true", help="execute mutating actions (needs --by)")
    p.add_argument("--by", help="approver; must differ from --who")
    p.add_argument("--who", default=getpass.getuser(), help="invoking operator")
    a = p.parse_args(argv)

    if a.verify_ledger:
        bad = verify_ledger(a.ledger)
        print("ledger intact" if bad is None else f"ledger TAMPERED at line {bad}")
        return 0 if bad is None else 2
    if not (a.findings or a.demo):
        p.error("give --findings, --demo or --verify-ledger")
    if a.approve and (not a.by or a.by == a.who):
        p.error("--approve needs --by naming an approver other than --who (segregation of duties)")

    snap = json.loads(a.findings.read_text()) if a.findings else _demo_snapshot(a.demo)
    findings = analyze(snap)
    if not findings:
        print("no findings, nothing to do")
    failed = False
    for f in findings:
        e = process(f, ledger=a.ledger, who=a.who, approve=a.approve, by=a.by)
        ev = e["evidence"]
        print(f"{e['outcome'].upper():10s} {f.title}\n           owner: {e['owner']}  action: {e['action']}"
              + (f"  evidence: {ev['test'].split('::')[1]} {'pass' if ev['passed'] else 'FAIL'} {ev['seconds']}s" if ev else ""))
        failed |= e["outcome"] in ("refused", "unverified")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
