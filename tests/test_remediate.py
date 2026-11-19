import json
import subprocess
import sys

import pytest

from tools import remediate
from tools.triage import Finding

REDIS = Finding("warning", "Redis unavailable: pricing uncached", "redis=down", "Platform", "§3")
RESUBMIT = Finding("warning", "HTTP 5xx on pricing endpoints", "4 since pod start", "Core", "§2")
UNKNOWN = Finding("warning", "Cassandra unavailable: price log has a gap", "x", "Database", "§4")


def passed(test):
    return {"test": test, "passed": True, "seconds": 0.1}


def failed(test):
    return {"test": test, "passed": False, "seconds": 0.1}


def rows(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_unknown_finding_is_refused_and_logged(tmp_path):
    led = tmp_path / "l.jsonl"
    e = remediate.process(UNKNOWN, ledger=led, who="op", evidence_fn=pytest.fail)
    assert e["outcome"] == "refused" and e["action"] is None
    assert rows(led)[0]["outcome"] == "refused"


def test_mutating_without_approve_is_proposed_not_executed(tmp_path):
    e = remediate.process(RESUBMIT, ledger=tmp_path / "l.jsonl", who="op", evidence_fn=pytest.fail)
    assert e["outcome"] == "proposed" and e["evidence"] is None and e["approved_by"] is None


def test_approved_resubmit_executes_with_evidence(tmp_path):
    e = remediate.process(RESUBMIT, ledger=tmp_path / "l.jsonl", who="op", approve=True, by="alice",
                          evidence_fn=passed)
    assert e["outcome"] == "verified" and e["approved_by"] == "alice" and e["evidence"]["passed"]
    assert e["mutates"] is True and e["runbook_section"] == "§2"


def test_failed_evidence_is_unverified(tmp_path):
    e = remediate.process(REDIS, ledger=tmp_path / "l.jsonl", who="op", evidence_fn=failed)
    assert e["outcome"] == "unverified"


def test_tampered_ledger_is_detected(tmp_path):
    led = tmp_path / "l.jsonl"
    for _ in range(3):
        remediate.process(REDIS, ledger=led, who="op", evidence_fn=passed)
    assert remediate.verify_ledger(led) is None
    data = rows(led)
    data[1]["outcome"] = "edited-by-hand"
    led.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in data))
    assert remediate.verify_ledger(led) == 2


def test_evidence_argv_comes_only_from_allowlist(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0))
    hostile = Finding("warning", "Redis unavailable; rm -rf / && curl evil", "$(reboot)", "x; y", "§3")
    remediate.process(hostile, ledger=tmp_path / "l.jsonl", who="op")
    assert len(seen) == 1
    argv = seen[0]
    assert argv[:4] == [sys.executable, "-m", "pytest", "-q"]
    assert argv[-1] in {p["evidence_test"] for p in remediate.ALLOWLIST.values()}
    assert not any(bad in " ".join(argv) for bad in ("rm -rf", "evil", "reboot"))


def test_approve_needs_a_distinct_approver(tmp_path):
    base = ["--demo", "reset", "--approve", "--ledger", str(tmp_path / "l"), "--who", "a"]
    for extra in ([], ["--by", "a"]):
        with pytest.raises(SystemExit):
            remediate.main(base + extra)


def test_end_to_end_replays_a_real_siege_test(tmp_path):
    led = tmp_path / "l.jsonl"
    rc = remediate.main(["--demo", "reset", "--approve", "--by", "alice", "--who", "op", "--ledger", str(led)])
    e = rows(led)[0]
    assert rc == 0 and e["outcome"] == "verified" and e["evidence"]["passed"]
    assert remediate.verify_ledger(led) is None


def test_process_enforces_segregation_of_duties(tmp_path):
    """The approval rule holds for any caller, not only the CLI."""
    from tools import remediate
    finding = next(f for f in remediate.analyze(remediate._demo_snapshot("reset")) if remediate.kind_of(f))
    ledger = tmp_path / "ledger.jsonl"
    never = lambda test: (_ for _ in ()).throw(AssertionError("must not run evidence"))  # noqa: E731
    for by in (None, "op"):
        entry = remediate.process(finding, ledger=ledger, who="op", approve=True, by=by, evidence_fn=never)
        assert entry["outcome"] == "refused" and entry["evidence"] is None
