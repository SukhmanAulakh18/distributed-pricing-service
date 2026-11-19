"""A worker dies holding a task (S1), and a slow worker wakes up after its
task was already redone by someone else (S2)."""
import json
import threading

from service import taskqueue, worker
from service.cache import PriceCache
from tests.siege import harness as h
from tests.siege.harness import Killed, Watch
from tests.test_portfolio import book

BODY = book(9, include_trades=True)  # 3 tasks of 3 trades
HEADERS = {"Idempotency-Key": "siege-death", "Prefer": "respond-async"}
RECLAIMED = ("worker_reclaimed_total", {})
PENDING_ROUTE = "TasksStuckPending"


def kill(w, stream, mid, fields):
    raise Killed


def pending_owners(r):
    return [p["consumer"].decode() for p in r.xpending_range(taskqueue.INTRADAY, taskqueue.GROUP,
                                                              min="-", max="+", count=100)]


def test_worker_killed_mid_task(siege, r, make_client):
    baseline = siege.clean_book(BODY)
    taskqueue.ensure_groups(r)
    pool = siege.pool(r)
    with make_client(cache=PriceCache(r), queue=r) as c:
        job = c.post("/portfolio/price", json=BODY, headers=HEADERS).json()["job_id"]
        watch = Watch(RECLAIMED)
        pool.start("doomed", hook=kill)  # reads a task, dies before acking it
        pool.threads["doomed"].join(5)
        fault = siege.fault("worker killed while holding a claimed task", "another worker reclaims it; "
                            "book prices identical to the clean run", PENDING_ROUTE)
        held = pending_owners(r)
        pool.start("w1")
        pool.start("w2")
        got = siege.readback_book(c, job)
    diffs = h.diff(baseline, got)
    fault.finish(watch, f"held by {held} before recovery, {len(diffs)} diffs after, "
                 f"reclaimed +{watch.deltas()[h.Watch.label(*RECLAIMED)]:.0f}",
                 held_by_dead_worker=held == ["doomed"], identical_to_baseline=got is not None and not diffs,
                 reclaim_metric_moved=bool(watch.moved()), nothing_left_pending=not pending_owners(r))


def test_duplicate_delivery_after_reclaim(siege, r, make_client, monkeypatch):
    baseline = siege.clean_book(BODY)
    taskqueue.ensure_groups(r)
    real = worker.price_task

    def price(task, *a, **k):
        if threading.current_thread().name == "siege-late-error":
            raise RuntimeError("late duplicate blew up")
        return real(task, *a, **k)
    monkeypatch.setattr(worker, "price_task", price)

    gate, held, idx = threading.Event(), {}, {}

    def hold(name):
        def hook(w, stream, mid, fields):
            idx[name] = int(fields[b"idx"])
            held[name] = threading.Event()
            held[name].set()
            gate.wait(10)
        return hook

    pool = siege.pool(r)
    with make_client(cache=PriceCache(r), queue=r) as c:
        job = c.post("/portfolio/price", json=BODY, headers=HEADERS).json()["job_id"]
        watch = Watch(RECLAIMED, ("worker_tasks_total", {"kind": "portfolio", "outcome": "error"}))
        pool.start("late-ok", hook=hold("ok"))
        assert h.wait_for(lambda: "ok" in held)
        pool.start("late-error", hook=hold("error"))
        assert h.wait_for(lambda: "error" in held)
        fault = siege.fault("original worker finishes after its task was reclaimed and redone",
                            "late write is identical; a late error never replaces the good result",
                            PENDING_ROUTE)
        pool.start("live")
        got = siege.readback_book(c, job)
        before = r.hgetall(f"job:{job}:results")
        done_before = sum(h.sample("worker_tasks_total", kind="portfolio", outcome=o) for o in ("ok", "error"))
        gate.set()  # both late workers now run their duplicate and write
        assert h.wait_for(lambda: sum(h.sample("worker_tasks_total", kind="portfolio", outcome=o)
                                      for o in ("ok", "error")) >= done_before + 2)
        after = r.hgetall(f"job:{job}:results")
        final = siege.readback_book(c, job)

    def stable(raw):  # who priced it, when, and whether the cache answered legitimately differ
        out = {k: v for k, v in json.loads(raw).items() if k not in ("priced_by", "ms", "finished_at", "n_cached")}
        out["trades"] = [{k: v for k, v in t.items() if k != "cached"} for t in out["trades"]]
        return out
    ok_key, err_key = str(idx["ok"]).encode(), str(idx["error"]).encode()
    errors = h.sample("worker_tasks_total", kind="portfolio", outcome="error")
    fault.finish(watch, f"duplicate rewrote task {idx['ok']} identically, late error on task {idx['error']} "
                 f"left it untouched; {len(h.diff(baseline, final))} diffs",
                 first_run_matches_baseline=got is not None and not h.diff(baseline, got),
                 late_success_identical=stable(before[ok_key]) == stable(after[ok_key]),
                 late_error_did_not_replace=before[err_key] == after[err_key],
                 final_matches_baseline=final is not None and not h.diff(baseline, final),
                 late_error_was_counted=errors >= 1)
