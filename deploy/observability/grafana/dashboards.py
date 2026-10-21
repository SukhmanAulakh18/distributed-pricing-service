"""Dashboards as code: regenerates the JSON in grafana/dashboards/.

Run: python deploy/observability/grafana/dashboards.py
"""
import json
import os

OUT = os.path.join(os.path.dirname(__file__), "dashboards")
DS = {"type": "prometheus", "uid": "prom"}


def ts(title, exprs, x, y, unit="short", w=12, h=8, **defaults):
    return {"type": "timeseries", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit, **defaults}, "overrides": []},
            "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": legend} for i, (e, legend) in enumerate(exprs)]}


def stat(title, expr, x, y, unit="percentunit", w=6, h=4, decimals=2, thresholds=None):
    p = {"type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals}, "overrides": []},
         "targets": [{"refId": "A", "expr": expr}]}
    if thresholds:
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
        p["options"] = {"colorMode": "background"}
    return p


def dashboard(uid, title, panels, refresh="5s"):
    for i, p in enumerate(panels, 1):
        p["id"] = i
    return {"uid": uid, "title": title, "schemaVersion": 39, "refresh": refresh,
            "time": {"from": "now-15m", "to": "now"}, "panels": panels}


GOOD_BAD = lambda bad_below: [{"color": "red", "value": None}, {"color": "green", "value": bad_below}]  # noqa: E731

service = dashboard("pricing", "Pricing Service", [
    stat("Cache hit ratio (5m)", 'sum(rate(price_cache_total{result=~"hit.*"}[5m])) / sum(rate(price_cache_total[5m]))', 0, 0),
    stat("/price p99 (5m)", "histogram_quantile(0.99, sum by (le) (rate(price_latency_seconds_bucket[5m])))", 6, 0, unit="s"),
    stat("HTTP 5xx ratio (5m)", '(sum(rate(http_request_seconds_count{status=~"5.."}[5m])) or vector(0)) / sum(rate(http_request_seconds_count[5m]))', 12, 0),
    stat("Intraday backlog", 'max(pricing_queue_backlog{stream="pricing:intraday",kind="lag"})', 18, 0, unit="short", decimals=0),
    ts("Requests/s by route and status", [('sum by (route, status) (rate(http_request_seconds_count{route!~"/metrics|/health.*"}[1m]))', "{{route}} {{status}}")], 0, 4, "reqps"),
    ts("/price latency", [(f"histogram_quantile({q}, sum by (le) (rate(price_latency_seconds_bucket[1m])))", f"p{int(q * 100)}") for q in (0.5, 0.95, 0.99)], 12, 4, "s"),
    ts("Cache lookups/s", [("sum by (result) (rate(price_cache_total[1m]))", "{{result}}")], 0, 12, "ops"),
    ts("C++ compute time by product", [("histogram_quantile(0.99, sum by (le, product) (rate(price_compute_seconds_bucket[1m])))", "p99 {{product}}"),
                                       ("histogram_quantile(0.5, sum by (le, product) (rate(price_compute_seconds_bucket[1m])))", "p50 {{product}}")], 12, 12, "s"),
    ts("Worker tasks/s by kind and outcome", [("sum by (kind, outcome) (rate(worker_tasks_total[1m]))", "{{kind}} {{outcome}}")], 0, 20, "ops"),
    ts("Queue backlog by stream", [("max by (stream, kind) (pricing_queue_backlog)", "{{stream}} {{kind}}"),
                                   ("sum(rate(worker_reclaimed_total[1m]))", "reclaimed/s")], 12, 20),
    ts("Circuit breakers (0 closed, 1 half-open, 2 open)", [("max by (dependency) (dependency_circuit_state)", "{{dependency}}")], 0, 28, min=0, max=2, decimals=0),
    ts("Admission control and price log", [("sum(rate(compute_rejected_total[1m]))", "503 rejections/s"),
                                           ("sum(compute_in_flight)", "pricings in flight"),
                                           ("sum(rate(price_log_dropped_rows_total[1m]))", "log rows dropped/s"),
                                           ("sum(rate(price_store_errors_total[1m]))", "Cassandra write errors/s")], 12, 28),
])

slo = dashboard("pricing-slo", "Pricing SLOs", [
    stat("Availability, last 1h (SLO 99.9%)", "1 - sli:price_error_ratio:rate1h", 0, 0, decimals=3, thresholds=GOOD_BAD(0.999)),
    stat("Budget burn rate, 1h (page at 14.4x)", "sli:price_error_ratio:rate1h / 0.001", 6, 0, unit="none", decimals=1,
         thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 6}, {"color": "red", "value": 14.4}]),
    stat("Requests under 250 ms, 1h (SLO 99%)", "1 - sli:price_slow_ratio:rate1h", 12, 0, decimals=3, thresholds=GOOD_BAD(0.99)),
    stat("Books with no failed trades, 1h (SLO 99%)", "1 - (sli:book_failure_ratio:rate1h or vector(0))", 18, 0, decimals=3, thresholds=GOOD_BAD(0.99)),
    ts("Error-budget burn rate", [("sli:price_error_ratio:rate5m / 0.001", "5m window"), ("sli:price_error_ratio:rate1h / 0.001", "1h window"),
                                  ("vector(14.4)", "page threshold"), ("vector(6)", "ticket threshold")], 0, 4, "none"),
    ts("Slow-request ratio (over 250 ms)", [("sli:price_slow_ratio:rate5m", "5m"), ("sli:price_slow_ratio:rate1h", "1h"), ("vector(0.01)", "SLO 1%")], 12, 4, "percentunit"),
    {"type": "table", "title": "Firing alerts (routed by owner)", "datasource": DS, "gridPos": {"x": 0, "y": 12, "w": 24, "h": 8},
     "targets": [{"refId": "A", "expr": 'ALERTS{alertstate="firing"}', "format": "table", "instant": True}],
     "transformations": [{"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True, "__name__": True, "alertstate": True}}}]},
])

cluster = dashboard("pricing-cluster", "Pricing on Kubernetes", [
    ts("Ready replicas by workload (KEDA scales the worker pools)",
       [('kube_deployment_status_replicas_available{namespace="pricing"}', "{{deployment}}"),
        ('kube_statefulset_status_replicas_ready{namespace="pricing"}', "{{statefulset}}")], 0, 0, "none", w=24, decimals=0),
    ts("Node Ready", [('kube_node_status_condition{condition="Ready",status="true"}', "{{node}}")], 0, 8, "none", min=0, max=1, decimals=0),
    ts("Pods per node (pricing namespace)", [('count by (node) (kube_pod_info{namespace="pricing"})', "{{node}}")], 12, 8, "none", decimals=0),
    ts("Container restarts (5m)", [('sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="pricing"}[5m])) > 0', "{{pod}}")], 0, 16, "none"),
    ts("Queue lag that drives KEDA", [('max by (stream) (pricing_queue_backlog{kind="lag"})', "{{stream}}")], 12, 16, "none"),
])

for d in (service, slo, cluster):
    with open(os.path.join(OUT, f"{d['uid']}.json"), "w") as f:
        json.dump(d, f, indent=2)
print("wrote", [d["uid"] for d in (service, slo, cluster)])
