"""Incidents 10 and 14 (chaos run 3): the client loses its connection, retries
the book, and must get the same job back, not a second pricing."""
from service import taskqueue
from service.cache import PriceCache
from tests.siege import harness as h
from tests.siege.harness import Watch
from tests.test_portfolio import book

BODY = book(9, include_trades=True)
KEY = {"Idempotency-Key": "siege-reset"}


class Recorder:
    def __init__(self):
        self.jobs = []

    def record_job(self, row):
        self.jobs.append(row)

    def write_prices(self, rows):
        pass


def test_client_reset_then_retry_with_same_key(siege, r, make_client):
    baseline = siege.clean_book(BODY)
    taskqueue.ensure_groups(r)
    store, pool = Recorder(), siege.pool(r)
    watch = Watch(("jobs_total", {"kind": "portfolio", "mode": "distributed", "outcome": "complete"}))
    with make_client(cache=PriceCache(r), queue=r, store=store) as c:
        fault = siege.fault("client abandons its book requests mid-flight, then retries with the same key",
                            "one job, one set of tasks, one record; the retry returns the baseline result",
                            "BookFailureFastBurn")
        c.post("/portfolio/price", json=BODY, headers={**KEY, "Prefer": "respond-async"})  # response lost
        gave_up = c.post("/portfolio/price", json={**BODY, "timeout_s": 0.05}, headers=KEY)  # client timed out
        jobs = {k.decode().split(":")[1] for k in r.keys("job:*:meta")}
        pool.start("w1")
        pool.start("w2")
        assert h.wait_for(lambda: taskqueue.done(r, h.job_id_for("siege-reset")) == baseline["tasks"])
        retries = [c.post("/portfolio/price", json={**BODY, "timeout_s": 20}, headers=KEY) for _ in range(3)]
    views = [h.book_view(x.json()) for x in retries]
    fault.finish(watch, f"first attempts: 202 then {gave_up.status_code}; {len(jobs)} job id; retries "
                 f"{[x.status_code for x in retries]}; recorded {len(store.jobs)}x",
                 first_attempts_made_one_job=jobs == {h.job_id_for("siege-reset")},
                 retries_return_same_job=all(x.json()["job_id"] in jobs for x in retries),
                 retries_identical_to_baseline=all(not h.diff(baseline, v) for v in views),
                 enqueued_once=r.xlen(taskqueue.INTRADAY) == baseline["tasks"],
                 recorded_once=len(store.jobs) == 1,
                 completion_metric_moved_exactly_once=watch.deltas()[h.Watch.label(*watch.specs[0])] == 1)
