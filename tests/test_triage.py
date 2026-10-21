from datetime import datetime, timezone

from tools.triage import analyze, parse_metrics

HEALTHY = {"health": {"ready": True, "status": "ok", "checks": {"redis": "ok", "cassandra": "ok"}},
           "queue": {"mode": "priority", "streams": {"pricing:intraday": {"lag": 0, "pending": 0},
                                                     "pricing:batch": {"lag": 0, "pending": 0}}},
           "jobs": [], "metrics": {}}


def test_all_clear():
    assert analyze(HEALTHY) == []


def test_parse_metrics_keeps_labels():
    text = '# HELP x\ndependency_circuit_state{dependency="redis"} 2.0\nprice_store_errors_total 3.0\n'
    m = parse_metrics(text)
    assert m['dependency_circuit_state{dependency="redis"}'] == 2.0
    assert m["price_store_errors_total"] == 3.0


def test_open_redis_circuit_routes_to_platform():
    snap = {**HEALTHY, "metrics": {'dependency_circuit_state{dependency="redis"}': 2.0}}
    (f,) = analyze(snap)
    assert f.owner.startswith("Platform") and f.runbook == "§3"


def test_cassandra_down_routes_to_database_team():
    snap = {**HEALTHY, "health": {"checks": {"redis": "ok", "cassandra": "down"}}}
    (f,) = analyze(snap)
    assert f.owner.startswith("Database") and "pricing unaffected" in f.title


def test_intraday_backlog_is_critical():
    snap = {**HEALTHY, "queue": {"streams": {"pricing:intraday": {"lag": 500, "pending": 8}}}}
    (f,) = analyze(snap)
    assert f.severity == "critical" and f.owner.startswith("Core")


def test_poison_trade_routes_to_quants():
    jobs = [{"job_id": "abcdef123456", "kind": "portfolio", "status": "partial", "n_failed": 1,
             "detail": '{"errors": ["exceeded max deliveries"]}'}]
    (f,) = analyze({**HEALTHY, "jobs": jobs})
    assert f.owner.startswith("Quant")


def pod(name, reason, finished):
    return {"metadata": {"name": name}, "status": {"phase": "Running", "containerStatuses": [
        {"restartCount": 4, "ready": True,
         "lastState": {"terminated": {"reason": reason, "exitCode": 137, "finishedAt": finished}}}]}}


NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def test_recent_oom_kill_is_critical():
    (f,) = analyze({**HEALTHY, "pods": [pod("pricer-worker-1", "OOMKilled", "2026-10-21T11:50:00Z")]}, now=NOW)
    assert f.severity == "critical" and "OOM" in f.title and f.owner.startswith("Core")


def test_old_restarts_are_history_not_findings():
    assert analyze({**HEALTHY, "pods": [pod("pricer-api-1", "Error", "2026-10-20T08:00:00Z")]}, now=NOW) == []


def test_restarts_route_to_the_owning_team():
    (f,) = analyze({**HEALTHY, "pods": [pod("cassandra-0", "Error", "2026-10-21T11:30:00Z")]}, now=NOW)
    assert f.owner.startswith("Database")


def test_api_down_short_circuits():
    (f,) = analyze({"health": {"error": "connection refused"}})
    assert f.severity == "critical"
