"""Siege: replay the platform's past incidents as automated attacks and
prove recovery by reading the results back, not by trusting an "ok".

Each scenario does the same three things:

1. Baseline. Run the same request on a clean, separate Redis with healthy
   workers and keep what the system itself reports (prices, exposures).
2. Attack. Inject the incident into a running in-process system (workers on
   fakeredis behind the real FastAPI app) and let it recover on its own.
3. Verify three ways. Recovered: the final result, read back through the
   API, equals the baseline. Detected: the Prometheus metric an operator
   would watch moved in the in-process registry. Routed: the alert for that
   symptom exists in the alert rules with the owner who should get it.

Every injected fault leaves one entry in a ledger (SCORECARD), which the
session fixture in conftest.py writes to bench/results/siege.json.
"""
import hashlib
import json
import math
import threading
import time
from pathlib import Path

import fakeredis
import redis
import yaml
from prometheus_client import REGISTRY

from service import taskqueue
from service.cache import PriceCache
from service.store import NullStore
from service.worker import Worker

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy" / "observability" / "rules" / "pricing-rules.yml"
SCORECARD: list[dict] = []


class Killed(BaseException):
    """A worker process dying: BaseException, so Worker's own `except
    Exception` cannot turn it into an error result and an ack."""


class SiegeWorker(Worker):
    """The real Worker with a hook before each task (to hold or kill it) and
    a short read block so tests stop quickly."""

    def __init__(self, *a, hook=None, **k):
        super().__init__(*a, **k)
        self.hook = hook

    def run_once(self, block_ms: int = 50) -> int:
        return super().run_once(block_ms)

    def handle(self, stream, message_id, fields):
        if self.hook:
            self.hook(self, stream, message_id, fields)
        super().handle(stream, message_id, fields)


class Pool:
    """Worker threads running the real Worker.run() loop (read, reclaim,
    redis-error backoff). `supervised` restarts a worker that dies, as
    Kubernetes restarts a pod."""

    def __init__(self, r, store=None):
        self.r, self.store = r, store or NullStore()
        self.workers: dict[str, SiegeWorker] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.deaths = 0

    def start(self, name, hook=None, supervised=False, cache=None):
        w = SiegeWorker(self.r, cache or PriceCache(self.r), self.store, consumer=name, hook=hook)

        def loop():
            while not w.stopping:
                try:
                    w.run()
                except Killed:
                    self.deaths += 1
                    if not supervised:
                        return
        t = threading.Thread(target=loop, name=f"siege-{name}", daemon=True)
        self.workers[name], self.threads[name] = w, t
        t.start()
        return w

    def stop(self):
        for w in self.workers.values():
            w.stopping = True
        for t in self.threads.values():
            t.join(timeout=5)


class Flaky:
    """A Redis client that raises ConnectionError while `down` is set, for
    both direct calls and pipelines."""

    def __init__(self, inner, ctl=None):
        self._inner, self.down = inner, ctl or threading.Event()

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            if self.down.is_set():
                raise redis.ConnectionError("siege: redis outage")
            out = attr(*a, **k)
            return Flaky(out, self.down) if name == "pipeline" else out
        return call

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Watch:
    """Metric samples taken now, compared later: which signals moved."""

    def __init__(self, *specs):
        self.specs = [(n, dict(lb)) for n, lb in specs]
        self.before = [sample(n, **lb) for n, lb in self.specs]

    @staticmethod
    def label(name, lb):
        return name + ("{" + ",".join(f"{k}={v}" for k, v in lb.items()) + "}" if lb else "")

    def deltas(self) -> dict[str, float]:
        return {self.label(n, lb): sample(n, **lb) - b for (n, lb), b in zip(self.specs, self.before, strict=True)}

    def moved(self) -> list[str]:
        return [k for k, v in self.deltas().items() if v]


def alert_owner(alert: str) -> str:
    rules = yaml.safe_load(RULES.read_text())
    for g in rules["groups"]:
        for rule in g["rules"]:
            if rule.get("alert") == alert:
                return rule["labels"]["owner"]
    raise AssertionError(f"alert {alert} is not in {RULES.name}")


def job_id_for(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def book_view(out: dict) -> dict:
    """What a book must reproduce exactly; job id, timings and who priced it
    legitimately differ between runs."""
    keep = ("n_trades", "n_failed", "pv", "pv_std_err", "delta_by_underlying", "vega_by_underlying", "tasks")
    rows = sorted(({k: t.get(k) for k in ("trade_id", "price", "std_err", "delta", "vega", "error")}
                   for t in out.get("trades") or []), key=lambda t: t["trade_id"])
    return {**{k: out[k] for k in keep}, "trades": rows}


def diff(expected, observed, path="") -> list[str]:
    """Exact differences between two JSON-like values (none means identical)."""
    if isinstance(expected, dict) and isinstance(observed, dict):
        return [d for k in sorted(set(expected) | set(observed))
                for d in diff(expected.get(k), observed.get(k), f"{path}.{k}")]
    if isinstance(expected, list) and isinstance(observed, list) and len(expected) == len(observed):
        return [d for i, (a, b) in enumerate(zip(expected, observed, strict=True)) for d in diff(a, b, f"{path}[{i}]")]
    return [] if expected == observed else [f"{path}: {expected!r} != {observed!r}"]


def diff_exposure(local: dict, got: dict) -> list[str]:
    """PFE is an order statistic, so it must match bit for bit; EE and CVA
    are sums, so block order may move them by rounding only."""
    out = [] if local["pfe"] == got["pfe"] else ["pfe differs"]
    if len(local["ee"]) != len(got["ee"]):
        out.append("ee length")
    out += [f"ee[{i}]" for i, (a, b) in enumerate(zip(local["ee"], got["ee"], strict=False))
            if not math.isclose(a, b, rel_tol=1e-12)]
    if not math.isclose(local["cva"], got["cva"], rel_tol=1e-12):
        out.append("cva differs")
    return out


class Fault:
    def __init__(self, siege, name, expected, alert):
        self.siege, self.name, self.expected, self.alert = siege, name, expected, alert
        self.t = time.monotonic()
        self.at_s = self.t - siege.t0

    def finish(self, watch: Watch, observed: str, **checks):
        """Records the ledger entry, then fails the test if any check failed."""
        recovered_s = time.monotonic() - self.t
        failed = [k for k, ok in checks.items() if not ok]
        SCORECARD.append({
            "scenario": self.siege.scenario, "fault": self.name, "at_s": round(self.at_s, 3),
            "expected": self.expected, "observed": observed if not failed else f"{observed}; FAILED {failed}",
            "recovered_s": round(recovered_s, 3), "passed": not failed,
            "detected_by": sorted(watch.moved()), "routed_to": alert_owner(self.alert), "alert": self.alert})
        assert not failed, f"{self.name}: {failed}; {observed}"


class Siege:
    def __init__(self, scenario, make_client, cleanup):
        self.scenario, self.make_client, self.cleanup = scenario, make_client, cleanup
        self.t0 = time.monotonic()

    def pool(self, r, **k) -> Pool:
        p = Pool(r, **k)
        self.cleanup.append(p.stop)
        return p

    def fault(self, name, expected, alert) -> Fault:
        return Fault(self, name, expected, alert)

    def clean_book(self, body: dict) -> dict:
        """The same book on its own empty Redis with two healthy workers."""
        r = fakeredis.FakeRedis(server=fakeredis.FakeServer())
        taskqueue.ensure_groups(r)
        pool = self.pool(r)
        pool.start("base1")
        pool.start("base2")
        with self.make_client(cache=PriceCache(r), queue=r) as c:
            out = c.post("/portfolio/price", json={**body, "timeout_s": 20}).json()
        pool.stop()
        assert out["n_failed"] == 0, "baseline book must be clean"
        return book_view(out)

    def clean_exposure(self, req: dict) -> dict:
        """The single local run every distributed exposure must reproduce."""
        from service import main
        from service.models import ExposureRequest
        return main._exposure_inline(ExposureRequest(**req)).model_dump()

    def readback_book(self, c, job_id: str, timeout_s: float = 15) -> dict | None:
        """Polls the job until the API can assemble it, returns the book view."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = c.get(f"/jobs/{job_id}")
            if resp.status_code == 200 and resp.json().get("complete"):
                return book_view(resp.json()["result"])
            time.sleep(0.03)
        return None

    def readback_exposure(self, c, job_id: str, timeout_s: float = 20) -> dict | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            body = c.get(f"/risk/exposure/{job_id}").json()
            if body["status"] != "running":
                return body
            time.sleep(0.05)
        return None


def wait_for(cond, timeout_s: float = 10) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def write_scorecard(path: Path) -> None:
    if not SCORECARD:
        return
    passed = sum(1 for s in SCORECARD if s["passed"])
    doc = {"scenarios": SCORECARD, "passed": passed, "failed": len(SCORECARD) - passed,
           "generated_by": "pytest tests/siege"}
    path.write_text(json.dumps(doc, indent=1) + "\n")
