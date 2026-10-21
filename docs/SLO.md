# Service level objectives

What "working" means for this service, how it is measured, and when someone
gets paged. The rules live in
[`deploy/observability/rules/pricing-rules.yml`](../deploy/observability/rules/pricing-rules.yml),
validated with `promtool`, and the **Pricing SLOs** Grafana dashboard shows
them live.

## Objectives

| SLO | SLI (how it is measured) | Target | Error budget (30 days) |
| --- | --- | --- | --- |
| **Price availability** | Share of `/price` requests that do not return 5xx (`http_request_seconds_count`) | 99.9% | 43 minutes of full outage, or 0.1% of requests |
| **Price latency** | Share of `/price` requests completing within 250 ms (`price_latency_seconds` histogram) | 99% | 1% of requests may be slower |
| **Book completeness** | Share of books that complete with no failed trades (`jobs_total{kind="portfolio"}`) | 99% | 1 in 100 books may need a retry |

Why these: a trader cares that a price comes back, quickly, and that a book
revaluation is whole. Exposure runs are batch: their timeliness is
watched through queue alerts, not an SLO.

## Alerting on burn rate, not on thresholds

A fixed error-rate threshold either pages on harmless blips or misses a slow
leak. These alerts fire on how fast the error budget is being spent, over two
windows at once (the multi-window approach from Google's SRE workbook):

| Alert | Fires when | Means | Severity |
| --- | --- | --- | --- |
| `PriceAvailabilityFastBurn` | error ratio > 14.4 × 0.1% over **1 h and 5 m** | 2% of the month's budget gone in an hour | page |
| `PriceAvailabilitySlowBurn` | error ratio > 6 × 0.1% over **6 h and 30 m** | a slow leak that would exhaust the budget in 5 days | ticket |
| `PriceLatencyBurn` | slow ratio > 14.4 × 1% over **1 h and 5 m** | latency budget burning fast | page |

The long window proves the problem is real; the short one proves it is
still happening, so the alert also clears quickly once fixed.

## Component alerts and who gets them

Every alert carries an `owner` label. Alertmanager routes on it, and every
alert links to its runbook section. It's the same ownership table as
[RUNBOOK.md](../RUNBOOK.md) and `tools/triage.py`.

| Alert | Owner | Why it exists |
| --- | --- | --- |
| `IntradayBacklog` | core | intraday tasks are waiting for a worker |
| `TasksStuckPending` | core | delivered tasks not acked for 2 minutes: no live worker to reclaim them |
| `AdmissionRejecting` | core | API pods shedding load with 503s: a capacity question |
| `ApiReplicasUnavailable`, `PodRestarting` | core | fewer than 2 API pods, or crash loops |
| `RedisCircuitOpen`, `NodeNotReady` | platform | infrastructure under the service is down |
| `CassandraCircuitOpen`, `CassandraReplicaDown`, `PriceLogDropping` | database | the price log has a gap, or quorum is at risk |
| `PoisonTrade` | quant | a trade crashed the pricer three times |

Inhibition rules suppress the alerts a root cause creates: when Redis or a
node is down, the backlog it causes does not page anyone separately.

## What the chaos runs showed

During the 10-minute chaos runs (11 faults including a whole Kubernetes
node; see [PERFORMANCE_REVIEW.md](PERFORMANCE_REVIEW.md), finding 11) the
availability SLI stayed above 99.99%. The alerts that fired were the
component ones, each routed to its owner: Cassandra replica down → database,
pods restarting → core engineering, node NotReady and the Redis circuit
open → platform. The availability burn-rate alerts did not fire, which is
correct: the budget was never at risk.

Two lessons from getting here, both in finding 14:

- **The node alert needs monitoring outside the node.** In an earlier run
  kube-state-metrics was on the stopped node and NodeNotReady never fired.
  The observability stack now runs on the control plane.
- **An SLI with no errors must read zero, not empty.** The error-ratio
  recording rules use `(sum(rate(...5xx...)) or vector(0))`; without it the
  availability panel showed "No data" exactly when nothing was failing.

The latency SLO is the one this laptop struggles with. With the whole
stack, Rancher and 8 API pods sharing 10 vCPUs, about 6% of prices took
over 250 ms in a busy hour. That burns latency budget, but below the 14.4×
rate that pages, so it shows on the dashboard rather than waking anyone.
On real hardware, the fix is capacity, not a looser objective.
