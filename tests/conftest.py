import math
import threading

import fakeredis
import pytest
from fastapi.testclient import TestClient

from service import main, taskqueue
from service.cache import PriceCache
from service.store import NullStore
from service.worker import Worker


def black_scholes(spot, strike, rate, vol, maturity, is_call=True, div_yield=0.0):
    """Independent Python reference for cross-checking the C++ library."""
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))  # noqa: E731
    d1 = (math.log(spot / strike) + (rate - div_yield + 0.5 * vol ** 2) * maturity) / (vol * math.sqrt(maturity))
    d2 = d1 - vol * math.sqrt(maturity)
    fwd, pv_k = spot * math.exp(-div_yield * maturity), strike * math.exp(-rate * maturity)
    return fwd * n(d1) - pv_k * n(d2) if is_call else pv_k * n(-d2) - fwd * n(-d1)


@pytest.fixture
def r():
    return fakeredis.FakeRedis()


@pytest.fixture
def make_client():
    def make(cache=None, store=None, queue=None):
        main.app.state.cache = cache or PriceCache(None)
        main.app.state.store = store or NullStore()
        main.app.state.queue = queue
        return TestClient(main.app)
    return make


@pytest.fixture
def workers(r):
    """Starts in-process workers on the fake Redis; stops them after the test."""
    stop = threading.Event()
    threads = []

    def start(names=("w1", "w2"), store=None):
        taskqueue.ensure_groups(r)
        for name in names:
            w = Worker(r, PriceCache(r), store or NullStore(), consumer=name)

            def loop(w=w):
                while not stop.is_set():
                    w.run_once(block_ms=20)
            t = threading.Thread(target=loop, daemon=True)
            t.start()
            threads.append(t)

    yield start
    stop.set()
    for t in threads:
        t.join(timeout=5)
