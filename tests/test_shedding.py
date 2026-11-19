"""Risk quotes shed at once when their slot is busy, and readiness keeps
answering: the regression test for the burst the review panel measured
(300 concurrent quotes, none shed, 36 s worst case, readiness timing out)."""
import threading
import time

from service import main
from tests.test_predeal import NEW, REQ


def test_burst_is_shed_fast_and_readiness_stays_up(make_client, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow_quote(body):
        started.set()
        release.wait(10)  # hold the only risk slot
        return {"held": True}

    monkeypatch.setattr(main, "_predeal", slow_quote)
    body = {"netting_set": REQ, "new_trade": NEW}
    with make_client() as c:
        first = {}
        holder = threading.Thread(target=lambda: first.update(r=c.post("/risk/predeal", json=body)), daemon=True)
        holder.start()
        assert started.wait(5)
        t0 = time.perf_counter()
        burst = [c.post("/risk/predeal", json=body) for _ in range(20)]
        shed_s = time.perf_counter() - t0
        ready = c.get("/health/ready")
        sens = c.post("/risk/cva-sensitivities", json={"netting_set": REQ})  # shares the risk budget
        release.set()
        holder.join(10)
    assert all(r.status_code == 503 and r.headers["Retry-After"] for r in burst)
    assert shed_s < 2.0  # 20 rejections, none waits for the slot
    assert ready.status_code == 200
    assert sens.status_code == 503
