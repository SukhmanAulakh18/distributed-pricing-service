# Runbook: Pricing Service

How to triage the pricing service intraday. Every symptom below is one I
either hit while building this or reproduced on purpose with
`bench/failure_test.sh`, `bench/worker_failure.py` or by stopping a
dependency.

## Start here

```bash
python -m tools.triage --api http://localhost:8080 --namespace pricing   # or: make triage
```

It reads health, queue backlog, metrics, today's job history (Cassandra) and
pod state, and prints findings, each with an owner and a section of this
runbook. Exit code 0 is all clear, 1 warnings, 2 critical. Example during a
Redis and Cassandra outage:

```
[WARNING ] Redis unavailable: pricing uncached, books priced on the API pods
           evidence: redis=down, circuit=2
           route to: Platform (Kubernetes, Redis)  (RUNBOOK §3)
[WARNING ] Cassandra unavailable: price log has a gap, pricing unaffected
           evidence: cassandra=down, write errors=40, dropped rows=0
           route to: Database team (Cassandra)  (RUNBOOK §4)
```

| Signal | Where |
| --- | --- |
| Dashboard | Grafana `Pricing Service` (compose: http://localhost:3000) |
| Health | `curl -s $API/health/ready`: `status: ok` or `degraded`, plus each dependency |
| Backlog | `curl -s $API/queue`: per stream, `lag` (not yet delivered) and `pending` (delivered, not acked) |
| Today's jobs | `curl -s $API/jobs` (from Cassandra) |
| One job | `curl -s $API/jobs/<job_id>`, then grep worker logs for the `job_id` |
| One request | Responses carry `x-request-id` and `x-served-by`; logs are JSON: `kubectl -n pricing logs <pod> \| grep <request_id>` |
| CPU throttling | `kubectl -n pricing exec <pod> -- cat /sys/fs/cgroup/cpu.stat` (`nr_throttled`) |

## Who owns what

| Component | Owner | Route to them when |
| --- | --- | --- |
| C++ pricer and numerics | Quant analytics | A price disagrees with its reference, NaNs, a crash inside `pricer_core`, a poison trade |
| API, workers, queue | Core engineering (this team) | Latency, errors, deploys, backlog, 503s |
| Redis | Platform | Redis down or evicting |
| Cassandra | Database team | Node down, write timeouts, full disk |
| Kubernetes cluster | Platform | Pods Pending, node NotReady, image pulls |

---

## 1. `/price` slow, or 503s

1. Grafana: compare **/price latency** with **C++ compute time**. Both up:
   the requests got bigger (check `n_paths` and fixings), or the pods are
   short of CPU. Only end-to-end up: go to step 2.
2. **Cache lookups/s**: hits falling right after a new
   `market_data_version` is expected (every key changes) and clears within
   the TTL.
3. `503` with `Retry-After`: admission control. Each API pod runs at most
   `COMPUTE_SLOTS` pricings at once (sized to its CPU limit) and rejects what
   waits longer than `ADMISSION_TIMEOUT_S`. Check the HPA
   (`kubectl -n pricing get hpa`); at `maxReplicas`, it is a capacity
   question for Platform.
4. Throttling: if `nr_throttled` climbs, the pod is running more threads
   than its CPU limit (performance review finding 6).

## 2. Errors (5xx, resets) or pods restarting

1. `kubectl -n pricing get pods`, then `describe` and `logs --previous` for
   the pod. `OOMKilled` means a request bigger than the memory budget got
   through; check the work caps in `service/models.py`.

**Expected:** crashing 1 of 3 API pods fails about 0.02% of requests over
about a third of a second; 2 of 3 fail about 0.2% over 0.7 s (measured with
16 clients, mostly cache misses). Errors lasting
much longer mean endpoints are not updating: `kubectl -n pricing get
endpoints pricer-api`.

## 3. Redis down

**Impact:** prices stay correct, but uncached. The circuit breaker skips
Redis after 3 failures, so requests do not wait out timeouts. Books fall back
to pricing on the API pods (`mode: local`). Exposure submissions return `503
batch queue (Redis) unavailable` rather than running inline.

**Act:** route to Platform. Nothing to do on our side; the breaker closes by
itself.

## 4. Cassandra down or slow

**Impact:** none on pricing. The price log has a gap: rows queued in the
write-behind buffer are written when Cassandra returns, or dropped and counted
(`price_log_dropped_rows_total`) if the buffer fills. `GET /jobs` returns
`503 job history unavailable`. Readiness is deliberately **not** gated on
Cassandra (incident 2).

**Act:** route to the Database team. Afterwards, the window between `circuit
opened` and `circuit closed` in the logs is the gap. Logged prices are
reproducible from their request hash, so the gap can be backfilled.

## 5. Books slow, partial (HTTP 206) or exposure runs stuck

1. `curl -s $API/queue`:
   - **Intraday lag growing**: workers cannot keep up. Check
     `kubectl -n pricing get hpa pricer-worker`, or scale manually.
   - **Batch lag growing**: normal during a big exposure run. Batch never
     delays intraday (performance review finding 4).
   - **Pending stuck above 0**: a worker died holding tasks. Others reclaim
     them after 10 s. If pending stays flat, no worker is alive.
2. A trade with `error: exceeded max deliveries` crashed or hung a worker 3
   times: a poison trade. Route it to Quant analytics with its parameters;
   do not resubmit it.
3. An exposure run with `payload expired or missing`: it sat queued longer
   than the job TTL (1 hour). Resubmit it.

**Expected:** SIGKILLing a worker mid-book loses no trades but adds roughly
the 10 s reclaim time; SIGTERM (normal deploys) adds under a second.

## 6. Pods restarted at startup

Usually a dependency was unavailable at boot: `kubectl -n pricing logs <pod>
--previous`. See incident 1. Restarts older than an hour are history, and
triage ignores them.

## 7. A Kubernetes node is lost

**Impact:** pods on that node stop answering. For about 50 s, until the node
controller marks the node NotReady, kube-proxy still sends some new
connections to them, which fail. After that:
- **Stateless pods** (API, workers) tolerate an unreachable node for only 30 s
  (`tolerationSeconds`), then are evicted and rescheduled on the surviving
  nodes. Tasks the dead workers held are reclaimed by other workers.
- **Stateful pods** are not rescheduled automatically. Their identity and
  disk are tied to the node. Cassandra keeps serving at quorum with 2 of 3
  replicas. A Redis on the lost node is down until the node returns (pricing
  degrades as in §3, and its queued work persists on its volume).

**Confirm:** `NodeNotReady` alert, `kubectl get nodes`, or Rancher → Nodes.

**Act:** route to Platform. Do not force-delete the Cassandra or Redis pods to
"move" them: their data is on the lost node's volume.

---

## Past incidents

All found by the failure tests or the audit, not by luck.

**1. API and worker pods in CrashLoopBackOff on a fresh deploy.** 4 to 5
restarts per pod while Cassandra started (`UnresolvableContactPoints`). A
headless Service has no DNS records until its pod is Ready, and the driver's
`Cluster()` resolves contact points eagerly, outside my retry. Fix: build the
cluster inside the retry. Verified with 0 restarts.

**2. A Cassandra outage would have taken down the whole API.** All three API
pods went not-ready together, so the Service had no endpoints. Readiness was
gated on a shared dependency, so every replica failed the same check at the
same moment. Fix: readiness reflects only the pod; dependencies are reported
as `degraded`.

**3. A Cassandra outage cut throughput.** 846 req/s instead of about 1,030,
and p99 114 ms instead of 40, as every request waited out write timeouts.
Fix: circuit breaker, and later the write-behind price log.

**4. A Redis outage stalled the API for a second.** 22 req/s and 859 ms p99
in the worst second, from several 200 ms socket timeouts per request. Fix:
circuit breaker on the cache. Worst second after the fix: 923 req/s, 60 ms.

**5. Kernel CPU throttling of the API pods.** 4 pricings × 2 threads = 8
threads against a 2-CPU limit; one pod was throttled in 16% of its CFS
periods. Fix: concurrency sized to the limit (1 thread × 2 slots). Throttled
periods fell from 6% to 0.06% under the same cache-heavy load; a
compute-bound mix still hits the quota, and there the fix is more pods
(performance review, finding 6).

**6. A malformed queue message crashed workers.** A message without the
expected fields (e.g. added with `redis-cli`) raised `KeyError` in the worker
loop. Unacked, it would be reclaimed and crash the next worker. Fix: such
messages are logged, acked and dropped.

**7. The triage tool reported old restarts as live problems.**
`restartCount` is cumulative, so yesterday's crash tests kept firing, and
Cassandra restarts were routed to Core engineering. Fix: only terminations
in the last hour count, routed by component.

**8. The price log silently dropped rows under load.** Grafana showed about
200 rows/s dropped from the write-behind queue during a mixed-load run, with
nothing else wrong. One-row batches were written one round trip at a time.
Fix: coalesce queued batches into one concurrent write. The same load then
dropped 0 rows. This is why `price_log_dropped_rows_total` is on the
dashboard and in triage.

**9. API pods killed during the first deploy of the 3-node Cassandra ring.**
The API connected to Cassandra during startup, and a ring takes minutes to
form, so the startup probe killed the pods (2 restarts each). Pricing does
not need Cassandra. Fix: connect in a background thread (`ConnectingStore`).
Pods start and price immediately, the price log waits in the write-behind
queue, and history endpoints answer 503 until the connection is up.

**10. Books failed at the connection level while a node was dying.** In the
first chaos run, 3 of 38 books failed with connection resets in the 50 s
before Kubernetes marked a stopped node NotReady; single prices lost only 1
request in 150,101. Books are idempotent (same input, same prices), so the
client retries a connection-level failure once. See the second chaos run in
the performance review, finding 11.

**11. The monitoring died with the node it was watching.** In the second
chaos run, a stopped worker node was hosting kube-state-metrics, so
`NodeNotReady` never fired. Fix: the whole observability stack runs on the
control-plane node, outside the workloads' failure domain (in production,
dedicated infra nodes). KEDA still runs on worker nodes, so autoscaling
pauses while its node is down; noted, not fixed.

**12. Rancher crash-looped under host CPU starvation.** During the same run,
Rancher shared the laptop's CPUs with a cluster under chaos plus screenshot
captures. Its embedded k3s exited, and a stale git `index.lock` in its chart
catalog then blocked every restart. Fix: remove the stale locks
(`find /var/lib/rancher-data/local-catalogs -name index.lock -delete` inside
the container). Lesson: the management plane belongs on its own cluster, so a
workload incident cannot take away the tool you need to manage it.

**13. The availability SLO read "No data" when nothing was failing.** The
error-ratio recording rules divided the 5xx rate by the total rate. With no
5xx responses at all, the numerator is an empty series, so the ratio, the
SLO panel and the burn-rate lines were all empty, not zero. An empty SLI
looks the same as broken monitoring. Fix: `(sum(rate(...5xx...)) or
vector(0)) / sum(rate(...))`.

**14. A third chaos run still lost one book.** With the retry from incident
10, 3 books were saved, but 1 of 30 failed on both attempts: both were
connection resets during the 2-minute node outage. A single HTTP retry is
too short for an outage that long. Proposed fix (performance review, next
steps): the client picks the job id and polls `/jobs/{id}` after a
connection failure, instead of re-sending the book.
