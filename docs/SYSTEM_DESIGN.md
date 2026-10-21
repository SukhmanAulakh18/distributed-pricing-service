# System design

The design in diagrams: what talks to what, how each request type moves
through the system, and the decisions behind it, along with what each one
costs. [ARCHITECTURE.md](ARCHITECTURE.md) has the component-level detail, and
[OPERATIONS.md](OPERATIONS.md) has the Kubernetes, Rancher and SRE view.

## Context

```mermaid
flowchart LR
  subgraph Clients
    T[Traders, e-trading]
    RK[Risk, batch]
  end
  T -->|"/price · /portfolio/price"| API
  RK -->|"/risk/exposure"| API
  subgraph Cluster["Kubernetes cluster"]
    API["API pods (3-8, HPA)<br/>FastAPI + Pydantic<br/>C++ via pybind11"]
    WI["Intraday workers (2-8)"]
    WB["Batch workers (0-6)"]
    KEDA["KEDA"]
    R[("Redis<br/>cache · streams · job state<br/>AOF")]
    C[("Cassandra<br/>3 nodes, RF 3")]
    O["Prometheus · Alertmanager<br/>Grafana · Jaeger"]
  end
  API <-->|"GET / SET NX"| R
  API -->|"XADD pricing:intraday"| R
  API -->|"XADD pricing:batch"| R
  R -->|XREADGROUP| WI
  R -->|XREADGROUP| WB
  WI -->|"results · XACK"| R
  WB -->|"results · XACK"| R
  KEDA -.->|reads stream lag| R
  KEDA -.->|scales| WI
  KEDA -.->|scales| WB
  API -.->|write-behind| C
  WI -.->|write-behind| C
  API & WI & WB -.->|"metrics, OTLP traces"| O
  Rancher["Rancher (outside the cluster)"] -.-|"cattle-cluster-agent connects out"| Cluster
```

## Single price: synchronous, latency matters

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant A as API pod
  participant R as Redis
  participant P as C++ pricer
  participant L as Cassandra (write-behind)
  C->>A: POST /price
  A->>A: validate (Pydantic, work caps), hash request
  A->>R: GET price:{hash}
  alt cache hit (~1 ms)
    R-->>A: cached price
  else cache miss
    A->>R: SET NX lock:{hash} (one pod computes)
    A->>P: price (GIL released, admission slot held)
    P-->>A: price, std error, Greeks
    A->>R: SET price:{hash} EX ttl
    A-)L: enqueue log row
  end
  A-->>C: 200 + x-trace-id
  Note over A,R: Redis down → circuit opens → priced uncached, still 200
```

## Book: distributed, intraday priority, survives a dead worker

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant A as API pod
  participant R as Redis
  participant W1 as Worker A
  participant W2 as Worker B
  participant L as Cassandra
  C->>A: POST /portfolio/price (2,000 trades)
  A->>R: HSET job · XADD 40 tasks (one pipeline, traceparent in each)
  R->>W1: XREADGROUP
  R->>W2: XREADGROUP
  W1->>R: MGET cached prices for the slice
  W1->>W1: one C++ batch call, GIL released
  W1->>R: pipelined SET · HINCRBY progress · XACK
  Note over W2: SIGKILL mid-task, message stays pending
  R->>W1: XAUTOCLAIM (idle > 10 s)
  W1->>R: same task, same prices, XACK
  W1-)L: write-behind price log
  loop until all tasks done
    A->>R: HGETALL job
  end
  A-->>C: 200 · PV, std error, delta and vega by underlying
```

A task delivered three times without success is marked failed in its job
and acked, and the `PoisonTrade` alert goes to the quant team. One bad
trade can't wedge the queue; the book still returns, with `n_failed`
counting the trades that could not be priced.

## Exposure run: batch, asynchronous, exact merge

```mermaid
sequenceDiagram
  autonumber
  participant C as Risk client
  participant A as API pod
  participant R as Redis
  participant W as Batch workers (0-6)
  participant K as KEDA
  C->>A: POST /risk/exposure (netting set, 100,000 scenarios)
  A->>R: store netting set once · XADD scenario blocks to pricing:batch
  A-->>C: 202 + job id
  K->>R: read lag on pricing:batch
  K->>W: scale 0 → N
  W->>R: per block: per-step sums + top-k exposures (float64 bytes)
  C->>A: GET /risk/exposure/{id}
  A->>R: all blocks present?
  A->>A: exact merge → EE, PFE (quantile), CVA
  A-->>C: profile + CVA (identical for any worker count)
```

## Why the answers are reproducible

```mermaid
flowchart LR
  Req[Request] --> H["seed = hash(request)<br/>(client id excluded)"]
  H --> P["Philox4x32-10<br/>key = seed, counter = path i"]
  P --> B1["block 1 on worker A"]
  P --> B2["block 2 on worker B"]
  P --> B3["block 3 re-run after a crash"]
  B1 & B2 & B3 --> M["exact merge<br/>(sums + top-k)"]
  M --> Same["bit-identical to 1 worker"]
```

A counter-based generator gives path *i* the same random numbers on any
thread, worker or node. Caching is therefore safe, sharding is exact, and
re-running a task after a crash is harmless. The chaos run relies on this
to check its own exposure results.

## Priority: before and after

```mermaid
flowchart LR
  subgraph Before["Before: one FIFO stream (intraday book 5.17 s)"]
    direction LR
    q1["B B B B B B I"] --> w1[workers]
  end
  subgraph After["After: two streams, two pools (0.32 s)"]
    direction LR
    qi["I"] --> pi[intraday pool]
    qb["B B B B B B"] --> pb[batch pool]
  end
```

## Decisions and their costs

| Decision | Instead of | Why | Cost accepted |
| --- | --- | --- | --- |
| Redis Streams + consumer groups | Redis list; Kafka | Per-message ack, reclaim (`XAUTOCLAIM`), lag readable by KEDA, one system fewer | Single Redis instance; the AOF survives restarts, not losing its node |
| Separate intraday and batch streams | One FIFO | Risk runs can't delay a trader's book (5.17 s → 0.32 s) | Two pools to size; cold start for batch |
| Results written before ack | Ack on receipt | At-least-once with idempotent re-runs | A task may be priced twice after a crash |
| Cassandra, write-behind | Postgres; synchronous writes | Append-heavy, time-bucketed, query-first, TTL, RF 3 | Log lags by up to ~1 s; a crash can drop the unwritten tail (metered and alerted) |
| KEDA on stream lag | HPA on CPU | Lag leads; CPU trails a queue | Another controller to run |
| Admission control sized to CPU limit | Unbounded concurrency | Fast 503 + `Retry-After` beats a slow timeout | Callers must retry |
| Observability on the control plane | Anywhere | Monitoring must not die with the node it watches | Needs infra nodes on a real cluster |
| Counter-based RNG keyed by request hash | Seeded per process | Reproducibility, safe caching, exact sharding | Request hashing must exclude non-economic fields |

## Capacity limits (by design)

- **Request work caps** in the Pydantic models: `n_paths × steps` is
  bounded, so a runaway request gets a 422, not an OOM-killed pod.
- **Admission:** a semaphore sized to the pod's CPU limit (`COMPUTE_SLOTS`).
  Beyond it, 503 with `Retry-After`, and the `AdmissionRejecting` alert
  goes to core engineering.
- **Write-behind queue:** bounded at 10,000 batches and coalesced into
  writes of up to 2,000 rows. Overflow drops the row (never the price),
  and `PriceLogDropping` goes to the database team.
