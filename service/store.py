import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Protocol

from prometheus_client import Counter, Gauge

from . import tracing
from .breaker import CircuitBreaker
from .retry import with_retry

log = logging.getLogger(__name__)

STORE_ERRORS = Counter("price_store_errors_total", "Failed Cassandra writes")
LOG_DROPPED = Counter("price_log_dropped_rows_total", "Price-log rows dropped because the write-behind queue was full")
LOG_QUEUED = Gauge("price_log_queued_batches", "Price-log batches waiting to be written")

# Query-first schema: one table per read pattern, no joins.
SCHEMA = [
    """
    CREATE KEYSPACE IF NOT EXISTS {ks}
    WITH replication = {{'class': 'NetworkTopologyStrategy', '{dc}': {rf}}}
    """,
    # "Latest prices for this instrument today". The day in the partition key
    # keeps partitions bounded; request_hash in the clustering key stops two
    # writes in the same millisecond from overwriting each other (upserts).
    # Intraday price log, so rows expire after 30 days.
    """
    CREATE TABLE IF NOT EXISTS {ks}.prices_by_day (
        trade_date date, instrument_id text, requested_at timestamp, request_hash text,
        price double, std_err double, delta double, vega double, n_paths int,
        method text, market_data_version text, job_id text,
        PRIMARY KEY ((trade_date, instrument_id), requested_at, request_hash)
    ) WITH CLUSTERING ORDER BY (requested_at DESC, request_hash ASC)
      AND default_time_to_live = 2592000
    """,
    # "What ran today and what failed": the triage tool's first query.
    """
    CREATE TABLE IF NOT EXISTS {ks}.job_runs (
        day date, submitted_at timestamp, job_id text, kind text, status text,
        n_tasks int, n_failed int, elapsed_ms double, workers int, detail text,
        PRIMARY KEY ((day), submitted_at, job_id)
    ) WITH CLUSTERING ORDER BY (submitted_at DESC, job_id ASC)
    """,
    # "Latest exposure profile for this netting set".
    """
    CREATE TABLE IF NOT EXISTS {ks}.exposure_runs (
        netting_set_id text, run_at timestamp, job_id text, market_data_version text,
        grid list<double>, ee list<double>, pfe list<double>,
        epe double, peak_pfe double, cva double, n_scenarios int,
        PRIMARY KEY ((netting_set_id), run_at)
    ) WITH CLUSTERING ORDER BY (run_at DESC)
    """,
]


# Schema evolution: additive only (new columns and tables), applied on every
# start and idempotent, because Cassandra has no transactional DDL and old
# and new pods run side by side during a rolling update.
MIGRATIONS = [
    "ALTER TABLE {ks}.prices_by_day ADD method text",
    "ALTER TABLE {ks}.prices_by_day ADD job_id text",
]


class Store(Protocol):
    def write_prices(self, rows: list[dict]) -> None: ...
    def latest_prices(self, instrument_id: str, limit: int) -> list[dict]: ...
    def record_job(self, job: dict) -> None: ...
    def recent_jobs(self, limit: int) -> list[dict]: ...
    def write_exposure(self, run: dict) -> None: ...
    def latest_exposure(self, netting_set_id: str) -> dict | None: ...
    def ping(self) -> bool: ...


class NullStore:
    """Used when no Cassandra hosts are configured (local dev, unit tests)."""

    def write_prices(self, rows): pass
    def latest_prices(self, instrument_id, limit): return []
    def record_job(self, job): pass
    def recent_jobs(self, limit): return []
    def write_exposure(self, run): pass
    def latest_exposure(self, netting_set_id): return None
    def ping(self): return True


class CassandraStore:
    def __init__(self, hosts: list[str], keyspace: str = "pricing", replication_factor: int = 1,
                 datacenter: str = "datacenter1", request_timeout: float = 2.0):
        from cassandra import ConsistencyLevel
        from cassandra.cluster import Cluster
        from cassandra.policies import DCAwareRoundRobinPolicy, TokenAwarePolicy

        self.breaker = CircuitBreaker("cassandra") if os.getenv("STORE_BREAKER", "on") == "on" else None

        # Cluster() resolves contact points eagerly. On Kubernetes, a headless
        # Service has no DNS records until the Cassandra pod is Ready, so
        # construction itself must be inside the retry, not just connect().
        def connect():
            # Token-aware: each statement goes straight to a replica that owns
            # its partition, instead of through a coordinator that forwards it.
            cluster = Cluster(hosts, load_balancing_policy=TokenAwarePolicy(DCAwareRoundRobinPolicy(local_dc=datacenter)),
                              protocol_version=5, connect_timeout=5)
            try:
                return cluster, cluster.connect()
            except Exception:
                cluster.shutdown()
                raise

        log.info("connecting to cassandra", extra={"hosts": hosts})
        # Runs in ConnectingStore's thread, so it can keep trying for as long
        # as the ring takes to come up.
        self.cluster, self.session = with_retry(connect, attempts=1_000_000, base_delay=1.0, max_delay=5.0)
        self.session.default_timeout = request_timeout
        from cassandra import InvalidRequest

        for stmt in SCHEMA:
            self.session.execute(stmt.format(ks=keyspace, rf=replication_factor, dc=datacenter), timeout=30)
        for stmt in MIGRATIONS:
            try:
                self.session.execute(stmt.format(ks=keyspace), timeout=30)
            except InvalidRequest as e:
                if "conflicts with an existing column" not in str(e) and "already exist" not in str(e):
                    raise
        self.session.set_keyspace(keyspace)

        def prepare(cql, consistency=ConsistencyLevel.LOCAL_ONE):
            stmt = self.session.prepare(cql)
            # Every write is an upsert on its full primary key, so the driver
            # may safely retry or speculatively re-send it.
            stmt.is_idempotent = True
            stmt.consistency_level = consistency
            return stmt

        # The price log tolerates LOCAL_ONE. Job status is what triage reads
        # to decide what failed, so it reads and writes at LOCAL_QUORUM.
        self._insert_price = prepare(
            "INSERT INTO prices_by_day (trade_date, instrument_id, requested_at, request_hash, price, "
            "std_err, delta, vega, n_paths, method, market_data_version, job_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        self._latest = prepare(
            "SELECT requested_at, request_hash, price, std_err, delta, vega, n_paths, method, "
            "market_data_version, job_id FROM prices_by_day WHERE trade_date = ? AND instrument_id = ? LIMIT ?")
        self._insert_job = prepare(
            "INSERT INTO job_runs (day, submitted_at, job_id, kind, status, n_tasks, n_failed, elapsed_ms, "
            "workers, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ConsistencyLevel.LOCAL_QUORUM)
        self._recent_jobs = prepare(
            "SELECT submitted_at, job_id, kind, status, n_tasks, n_failed, elapsed_ms, workers, detail "
            "FROM job_runs WHERE day = ? LIMIT ?", ConsistencyLevel.LOCAL_QUORUM)
        self._insert_exposure = prepare(
            "INSERT INTO exposure_runs (netting_set_id, run_at, job_id, market_data_version, grid, ee, pfe, "
            "epe, peak_pfe, cva, n_scenarios) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ConsistencyLevel.LOCAL_QUORUM)
        self._latest_exposure = prepare(
            "SELECT run_at, job_id, market_data_version, grid, ee, pfe, epe, peak_pfe, cva, n_scenarios "
            "FROM exposure_runs WHERE netting_set_id = ? LIMIT 1")

    def _exec(self, stmt, params):
        from cassandra import OperationTimedOut, ReadTimeout, Unavailable, WriteTimeout
        from cassandra.cluster import NoHostAvailable

        return with_retry(lambda: self.session.execute(stmt, params), attempts=3,
                          retry_on=(OperationTimedOut, Unavailable, WriteTimeout, ReadTimeout, NoHostAvailable))

    def _guarded(self, write):
        return write() if self.breaker is None else self.breaker.call(write)

    def write_prices(self, rows):
        """Writes a batch with up to 64 requests in flight. This is not a
        Cassandra BATCH: a multi-partition batch would funnel every row through
        one coordinator, while concurrent single-row writes spread across the
        ring and each go straight to its replicas."""
        from cassandra.concurrent import execute_concurrent_with_args

        if not rows:
            return
        now = datetime.now(timezone.utc)
        params = [(now.date(), r["instrument_id"], now, r["request_hash"], r["price"], r["std_err"],
                   r.get("delta"), r.get("vega"), r["n_paths"], r["method"], r["market_data_version"],
                   r.get("job_id")) for r in rows]

        def write():
            results = execute_concurrent_with_args(self.session, self._insert_price, params,
                                                   concurrency=64, raise_on_first_error=False)
            failed = sum(1 for ok, _ in results if not ok)
            if failed:
                raise RuntimeError(f"{failed} of {len(params)} price writes failed")

        self._guarded(write)

    def latest_prices(self, instrument_id, limit):
        today = datetime.now(timezone.utc).date()
        return [r._asdict() for r in self._exec(self._latest, (today, instrument_id, limit))]

    def record_job(self, job):
        submitted = datetime.fromtimestamp(job["submitted_at"], timezone.utc)
        params = (submitted.date(), submitted, job["job_id"], job["kind"], job["status"], job["n_tasks"],
                  job["n_failed"], job["elapsed_ms"], job["workers"], json.dumps(job.get("detail", {})))
        self._guarded(lambda: self._exec(self._insert_job, params))

    def recent_jobs(self, limit):
        today = datetime.now(timezone.utc).date()
        return [r._asdict() for r in self._exec(self._recent_jobs, (today, limit))]

    def write_exposure(self, run):
        params = (run["netting_set_id"], datetime.now(timezone.utc), run["job_id"], run["market_data_version"],
                  run["grid"], run["ee"], run["pfe"], run["epe"], run["peak_pfe"], run["cva"], run["n_scenarios"])
        self._guarded(lambda: self._exec(self._insert_exposure, params))

    def latest_exposure(self, netting_set_id):
        row = self._exec(self._latest_exposure, (netting_set_id,)).one()
        return row._asdict() if row else None

    def ping(self):
        try:
            self.session.execute("SELECT release_version FROM system.local", timeout=1.0)
            return True
        except Exception:
            return False


class WriteBehind:
    """Takes price-log writes off the pricing path.

    write_prices() enqueues and returns at once; a background thread writes to
    Cassandra. Before this, each portfolio task waited for its 100 inserts,
    leaving the worker's cores idle for about a quarter of every task (see
    docs/PERFORMANCE_REVIEW.md). If the queue fills, because Cassandra is
    slow or down, rows are dropped and counted rather than slowing pricing.
    Rows still queued are lost if the pod is killed; prices are reproducible
    from their request hash, so the log can be rebuilt. Everything other
    than write_prices goes straight to the wrapped store.
    """

    def __init__(self, store: Store, max_batches: int = 10_000, max_rows_per_write: int = 2_000):
        self.store = store
        self.max_rows_per_write = max_rows_per_write
        self.queue: queue.Queue = queue.Queue(maxsize=max_batches)
        self.thread = threading.Thread(target=self._drain, name="price-log", daemon=True)
        self.thread.start()

    def write_prices(self, rows: list[dict]) -> None:
        if not rows:
            return
        try:
            self.queue.put_nowait(rows)
        except queue.Full:
            LOG_DROPPED.inc(len(rows))
        LOG_QUEUED.set(self.queue.qsize())

    def _drain(self) -> None:
        while True:
            batches = [self.queue.get()]
            # Coalesce whatever else is already queued into one write. A
            # /price miss enqueues a single row; written one round trip at a
            # time, the log topped out near 500 rows/s and dropped rows under
            # load (seen on the Grafana dashboard). Together, the rows go out
            # with up to 64 writes in flight.
            rows = list(batches[0])
            while len(rows) < self.max_rows_per_write:
                try:
                    batch = self.queue.get_nowait()
                except queue.Empty:
                    break
                batches.append(batch)
                rows.extend(batch)
            try:
                # One span for the batch; the driver's per-row spans would
                # bury every other trace.
                with tracing.tracer.start_as_current_span("price_log.write", attributes={"rows": len(rows)}), \
                        tracing.quiet():
                    self.store.write_prices(rows)
            except Exception as e:
                log.error("price log write failed", extra={"rows": len(rows), "error": str(e)})
                STORE_ERRORS.inc()
            finally:
                for _ in batches:
                    self.queue.task_done()
                LOG_QUEUED.set(self.queue.qsize())

    def flush(self, timeout_s: float = 10.0) -> bool:
        """Waits for queued rows to be written (on shutdown). False on timeout."""
        deadline = time.monotonic() + timeout_s
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.queue.unfinished_tasks

    def __getattr__(self, name):
        return getattr(self.store, name)


class ConnectingStore:
    """Connects to Cassandra in a background thread, so pods start and price
    while the ring is still forming or down. Before this, the API connected
    during startup, and a 3-node ring takes minutes to form: the startup probe
    killed the pods. Until connected, ping() is False (health reports
    "degraded"), writes wait briefly (only the write-behind thread calls them;
    its queue absorbs the delay), and reads raise, which the API turns into a
    named 503."""

    def __init__(self, connect):
        self._store: Store | None = None
        self._ready = threading.Event()

        def run():
            self._store = connect()
            self._ready.set()
        threading.Thread(target=run, name="cassandra-connect", daemon=True).start()

    def write_prices(self, rows: list[dict]) -> None:
        if not self._ready.wait(timeout=5):
            raise RuntimeError("cassandra not connected yet")
        self._store.write_prices(rows)

    def ping(self) -> bool:
        return self._ready.is_set() and self._store.ping()

    def __getattr__(self, name):
        if not self._ready.is_set():
            raise RuntimeError("cassandra not connected yet")
        return getattr(self._store, name)


def build_store() -> Store:
    hosts = os.getenv("CASSANDRA_HOSTS")
    if not hosts:
        return NullStore()
    store = ConnectingStore(lambda: CassandraStore(
        hosts.split(","), keyspace=os.getenv("CASSANDRA_KEYSPACE", "pricing"),
        replication_factor=int(os.getenv("CASSANDRA_RF", "1")), datacenter=os.getenv("CASSANDRA_DC", "datacenter1")))
    return WriteBehind(store) if os.getenv("PRICE_LOG_MODE", "async") == "async" else store
