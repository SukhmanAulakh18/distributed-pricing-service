# Code audit

A review of the finished code, done before the final benchmarks. The rule:
every finding needs a concrete failing case, and the smallest fix wins
(preferably one that deletes code). Each finding below was
reproduced first, then fixed with a test that fails without the fix. The
"Status" lines were added after the fixes.

**What this repo does:** a C++ Monte Carlo and closed-form pricing library
behind a FastAPI service. Workers price books and run exposure jobs from
Redis Streams, Redis caches prices, and Cassandra keeps a price log, job
history and exposure runs. Deployed on Docker Compose and Kubernetes.
**Load assumed:** an internal service with tens of concurrent clients,
thousands of single prices per second, books of up to 100,000 trades, and
a few exposure runs at a time; no direct internet exposure.

## Must fix

1. **A malformed queue message crashes workers one after another** (`service/worker.py`, `handle`)
   - **What this is:** the worker loop reads task messages from Redis Streams and prices them.
   - **Problem:** it read `job`, `idx` and `kind` before its error handling. Reproduced:
     `XADD pricing:intraday * hello world` raised `KeyError: b'job'` out of the loop. Unacked, the
     message is reclaimed 10 s later by the next worker, which crashes too, until the poison limit.
   - **Fix:** parse the fields inside the error handling; log, ack and drop a message that is not ours.
   - **If we skip it:** one stray message from a debugging session restarts the whole worker pool.
   - **Status:** fixed; `test_malformed_message_is_dropped_not_fatal`.

2. **API pods throttled by their own configuration** (`deploy/k8s/base/api.yaml`)
   - **What this is:** each API pod has a 2-CPU limit and runs up to `COMPUTE_SLOTS` pricings with
     `PRICER_THREADS` threads each.
   - **Problem:** 4 slots × 2 threads = 8 busy threads on 2 CPUs. The kernel's CFS quota then stalls
     every thread of the pod until the next 100 ms period. Measured with `cpu.stat`: one pod was
     throttled in 627 of 3,950 periods (16%).
   - **Fix:** size concurrency to the limit: 1 thread × 2 slots on the API (many small requests),
     2 threads on workers (big tasks).
   - **If we skip it:** periodic stalls that show up only as tail latency, the hardest kind to trace.
   - **Status:** fixed. Throttled periods fell from 6% to 0.06% under identical load. Latency did not
     improve on the single-node cluster (performance review, finding 6), but the sizing is right for
     real nodes.

3. **NaN standard error at the smallest legal path count** (`core/src/pricing.cpp`, `check`)
   - **What this is:** the C++ API accepts `n_paths >= 2`.
   - **Problem:** with antithetic variates, 2 paths are 1 sample, so the variance divides by
     n − 1 = 0. `price_european_mc(..., n_paths=2, antithetic=True)` returned `std_err = nan`.
   - **Fix:** require at least 2 samples (4 paths with antithetic variates).
   - **If we skip it:** a NaN error bar reaches a caller that trusts it. The service's 1,000-path
     minimum hid it, but the library is called directly too.
   - **Status:** fixed; `Pricing.RejectsInvalidInputs`.

## Should fix

4. **Dead feature: market-data snapshot endpoints** (`service/main.py`, `models.py`, `store.py`)
   - **What this is:** `PUT /market-data` and `GET /market-data/{version}/{symbol}` with their own
     Cassandra table.
   - **Problem:** nothing prices from them (requests carry their own market data), no test covers
     them, and they were the only reason for one table and two prepared statements.
   - **Fix:** delete them.
   - **If we skip it:** an untested write path and a table to migrate, for nothing.
   - **Status:** deleted (about 50 lines).

5. **`GET /jobs?limit=-1` blamed Cassandra** (`service/main.py`)
   - **What this is:** the triage tool's job-history endpoint.
   - **Problem:** a non-positive limit went to Cassandra, failed there, and came back as
     "job history unavailable", which blames the database for a bad request.
   - **Fix:** validate at the boundary (`Query(ge=1, le=500)`), so a bad limit gets a 422.
   - **Status:** fixed.

6. **The single-price path waited for its Cassandra write** (`service/pricing.py`). Found by the
   performance review just before the audit; listed so the audit is complete.
   - **What this is:** every cache miss appends a row to the price log.
   - **Problem:** the request waited for the insert, which was most of a cache miss's latency
     (p50 3.68 ms of which about 2.6 ms was the write).
   - **Fix:** a bounded write-behind queue drained by a background thread; rows are dropped and
     counted if Cassandra cannot keep up, never blocking pricing.
   - **Status:** fixed; p50 3.68 → 1.03 ms. `test_write_behind_never_blocks_and_counts_drops`.

## Nice to have

7. **Book waits poll Redis every 20 ms** (`service/taskqueue.py`, `wait`)
   - **What this is:** the API waits for a book's tasks by polling `HLEN`.
   - **Problem:** 50 polls a second per in-flight book. Fine at the assumed load (a few concurrent
     books); with hundreds of concurrent books it becomes noticeable Redis load.
   - **Fix (not done):** workers `RPUSH` a completion marker and the API `BLPOP`s with a timeout.
   - **Status:** left as is, noted here; the assumed load is far from the limit.

**Verdict:** healthy after fixes 1 to 6.
**Lean:** about 50 lines removed; no dependency could be dropped (each one carries a measured job).
**Not checked:** the prod Kustomize overlay on a real multi-node cluster (rendered, schema-checked
and server-side dry-run only), Rancher itself, and the Grafana dashboard queries beyond rendering.
