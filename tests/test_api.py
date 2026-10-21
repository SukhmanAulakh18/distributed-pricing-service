import threading

import fakeredis
import pytest
import redis

from service import pricing
from service.breaker import CircuitBreaker, CircuitOpen
from service.cache import PriceCache
from service.store import NullStore

from conftest import black_scholes

EURO = {"type": "european", "spot": 100, "strike": 105, "rate": 0.03, "vol": 0.2, "maturity": 1.0}
REQ = {"instrument_id": "SYN-001", "market_data_version": "md-2026-10-21-01", "product": EURO,
       "simulation": {"n_paths": 100_000}}


class RecordingStore(NullStore):
    def __init__(self):
        self.rows = []

    def write_prices(self, rows):
        self.rows.extend(rows)


class BrokenRedis:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise redis.ConnectionError("down")
        return fail


@pytest.fixture
def store():
    return RecordingStore()


@pytest.fixture
def client(make_client, store):
    with make_client(cache=PriceCache(fakeredis.FakeRedis()), store=store) as c:
        yield c


def test_european_mc_matches_closed_form_and_returns_greeks(client):
    r = client.post("/price", json=REQ).json()
    assert r["method"] == "mc" and r["n_paths"] == 100_000
    assert r["reference_price"] == pytest.approx(black_scholes(100, 105, 0.03, 0.2, 1.0), abs=1e-10)
    assert abs(r["price"] - r["reference_price"]) < 4 * r["std_err"]
    assert 0 < r["delta"] < 1 and r["vega"] > 0


def test_analytic_european_is_exact(client):
    r = client.post("/price", json={**REQ, "product": {**EURO, "method": "analytic"}}).json()
    assert r["std_err"] == 0 and r["method"] == "analytic"
    assert r["price"] == pytest.approx(black_scholes(100, 105, 0.03, 0.2, 1.0), abs=1e-10)
    assert r["gamma"] > 0


def test_asian_control_variate_and_bump_greeks(client):
    asian = {**EURO, "type": "asian", "n_fixings": 12}
    r = client.post("/price", json={**REQ, "product": asian, "simulation": {"n_paths": 50_000, "greeks": True}}).json()
    assert r["variance_reduction"] > 10
    assert 0 < r["delta"] < 1 and r["vega"] > 0


def test_miss_then_hit_and_one_log_row(client, store):
    first, second = client.post("/price", json=REQ).json(), client.post("/price", json=REQ).json()
    assert (first["cached"], second["cached"]) == (False, True)
    assert first["price"] == second["price"]
    assert len(store.rows) == 1


def test_same_contract_shares_the_cache_across_instrument_ids(client):
    client.post("/price", json=REQ)
    assert client.post("/price", json={**REQ, "instrument_id": "TRADE-999"}).json()["cached"] is True


def test_new_market_data_version_misses(client):
    client.post("/price", json=REQ)
    assert client.post("/price", json={**REQ, "market_data_version": "md-2"}).json()["cached"] is False


def test_deterministic_without_cache(make_client):
    with make_client() as c:
        a, b = c.post("/price", json=REQ).json(), c.post("/price", json=REQ).json()
    assert a["cached"] is b["cached"] is False
    assert a["price"] == b["price"]


def test_redis_down_still_prices_and_reports_degraded(make_client, store):
    with make_client(cache=PriceCache(BrokenRedis()), store=store) as c:
        assert c.post("/price", json=REQ).status_code == 200
        health = c.get("/health/ready")
        assert health.status_code == 200
        assert health.json()["status"] == "degraded"
        assert health.json()["checks"]["redis"] == "down"


def test_store_failure_does_not_fail_request(make_client):
    class Exploding(NullStore):
        def write_prices(self, rows):
            raise RuntimeError("cassandra down")
    with make_client(store=Exploding()) as c:
        assert c.post("/price", json=REQ).status_code == 200


@pytest.mark.parametrize("path, value", [
    (("product", "spot"), -1), (("product", "vol"), 6), (("product", "maturity"), 31),
    (("simulation", "n_paths"), 10), (("simulation", "n_paths"), 100_000_000), (("product", "rate"), 2),
])
def test_validation_422(client, path, value):
    body = {**REQ, "product": dict(EURO), "simulation": dict(REQ["simulation"])}
    body[path[0]][path[1]] = value
    r = client.post("/price", json=body)
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"][-1] == path[1]


def test_unknown_field_rejected(client):
    assert client.post("/price", json={**REQ, "seed": 1}).status_code == 422


def test_admission_control_returns_503_when_saturated(client, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    slots.acquire()  # every slot busy
    monkeypatch.setattr(pricing, "_slots", slots)
    monkeypatch.setattr(pricing, "ADMISSION_TIMEOUT_S", 0.05)
    r = client.post("/price", json=REQ)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "1"


def test_request_id_propagated(client):
    r = client.get("/health/live", headers={"x-request-id": "abc123"})
    assert r.headers["x-request-id"] == "abc123"
    assert "x-served-by" in r.headers


def test_metrics_exposed(client):
    client.post("/price", json=REQ)
    text = client.get("/metrics").text
    for name in ("price_requests_total", "price_compute_seconds", "dependency_circuit_state"):
        assert name in text


def test_circuit_breaker_fails_fast_then_recovers(monkeypatch):
    from service import breaker as breaker_mod
    clock = [0.0]
    monkeypatch.setattr(breaker_mod.time, "monotonic", lambda: clock[0])
    cb = CircuitBreaker("test", threshold=3, cooldown_s=10)
    calls = []

    def down():
        calls.append(1)
        raise RuntimeError("down")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            cb.call(down)
    assert cb.state == "open"
    with pytest.raises(CircuitOpen):
        cb.call(down)
    assert len(calls) == 3  # the open circuit never called the dependency
    clock[0] = 11
    assert cb.state == "half_open"
    assert cb.call(lambda: "ok") == "ok"
    assert cb.state == "closed"


def test_redis_breaker_skips_dead_redis():
    calls = []

    class CountingBroken:
        def __getattr__(self, name):
            def fail(*a, **k):
                calls.append(name)
                raise redis.ConnectionError("down")
            return fail

    cache = PriceCache(CountingBroken())
    for _ in range(10):
        assert cache.get("x") is None
    assert len(calls) == 3  # opened after 3 failures


def test_write_behind_never_blocks_and_counts_drops():
    import time as _time

    from service.store import WriteBehind

    gate = threading.Event()

    class SlowStore(NullStore):
        def __init__(self):
            self.rows = []

        def write_prices(self, rows):
            gate.wait()
            self.rows.extend(rows)

    slow = SlowStore()
    wb = WriteBehind(slow, max_batches=2)
    t0 = _time.perf_counter()
    for i in range(10):
        wb.write_prices([{"i": i}])
    assert _time.perf_counter() - t0 < 0.5  # enqueueing never waits on the store
    gate.set()
    assert wb.flush(5)
    assert 2 <= len(slow.rows) < 10  # what fit in the queue was written, the rest dropped
    assert wb.recent_jobs(5) == []  # other calls pass through


def test_dependency_outages_answer_503_not_500(make_client):
    class DeadStore(NullStore):
        def recent_jobs(self, limit):
            raise RuntimeError("cassandra down")

    with make_client(store=DeadStore(), queue=BrokenRedis()) as c:
        for path in ("/jobs", "/queue"):
            r = c.get(path)
            assert r.status_code == 503 and "unavailable" in r.json()["detail"]


def test_write_behind_coalesces_small_batches():
    from service.store import WriteBehind

    gate = threading.Event()

    class CountingStore(NullStore):
        def __init__(self):
            self.calls, self.rows = 0, 0

        def write_prices(self, rows):
            gate.wait()
            self.calls += 1
            self.rows += len(rows)

    store = CountingStore()
    wb = WriteBehind(store)
    for i in range(500):  # 500 single-row /price misses arriving while a write is in progress
        wb.write_prices([{"i": i}])
    gate.set()
    assert wb.flush(5)
    assert store.rows == 500
    assert store.calls <= 3  # not 500 round trips


def test_pods_start_and_price_before_cassandra_connects(make_client):
    from service.store import ConnectingStore, WriteBehind

    gate = threading.Event()
    connecting = ConnectingStore(lambda: (gate.wait(), RecordingStore())[1])  # a ring still forming
    store = WriteBehind(connecting)  # as build_store() wires it
    with make_client(store=store) as c:
        assert c.post("/price", json=REQ).status_code == 200  # pricing does not wait
        assert c.get("/health/ready").json()["checks"]["cassandra"] == "down"
        assert c.get("/jobs").status_code == 503
        gate.set()
        connecting._ready.wait(2)
        assert c.get("/health/ready").json()["checks"]["cassandra"] == "ok"
