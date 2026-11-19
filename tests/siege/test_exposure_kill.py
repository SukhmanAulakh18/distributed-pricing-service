"""A worker dies partway through a distributed exposure run."""
from service import taskqueue
from service.cache import PriceCache
from tests.siege import harness as h
from tests.siege.harness import Killed, Watch
from tests.test_exposure import REQ

BIG = {**REQ, "n_scenarios": 6_000, "block_size": 1_000}  # 6 blocks


def kill(w, stream, mid, fields):
    raise Killed


def test_exposure_run_with_a_worker_killed(siege, r, make_client):
    local = siege.clean_exposure(BIG)
    taskqueue.ensure_groups(r)
    pool = siege.pool(r)
    watch = Watch(("worker_reclaimed_total", {}),
                  ("worker_tasks_total", {"kind": "exposure", "outcome": "ok"}),
                  ("jobs_total", {"kind": "exposure", "mode": "distributed", "outcome": "complete"}))
    pool.start("doomed", hook=kill)
    with make_client(cache=PriceCache(r), queue=r) as c:
        job = c.post("/risk/exposure", json=BIG).json()["job_id"]
        pool.threads["doomed"].join(5)
        fault = siege.fault("worker killed holding an exposure block",
                            "block reclaimed; EE/CVA within 1e-12 and PFE bit-identical to the local run",
                            "TasksStuckPending")
        pool.start("w1")
        pool.start("w2")
        got = siege.readback_exposure(c, job)
    diffs = h.diff_exposure(local, got) if got and got["status"] == "complete" else ["not complete"]
    fault.finish(watch, f"{got and got['status']}, blocks by {len(got['workers_used'])} worker(s), diffs {diffs}",
                 matches_local_run=not diffs,
                 every_block_computed=watch.deltas()["worker_tasks_total{kind=exposure,outcome=ok}"] == 6,
                 reclaim_and_completion_metrics_moved=len(watch.moved()) == 3,
                 nothing_left_pending=not sum(b["pending"] for b in taskqueue.backlog(r).values()))
