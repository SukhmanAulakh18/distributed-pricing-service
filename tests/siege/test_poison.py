"""Incident 6 and its harder cousin: a malformed message, a task that makes
every worker that touches it die, and a task whose kind pricing rejects."""
from service import taskqueue
from service.cache import PriceCache
from tests.siege import harness as h
from tests.siege.harness import Killed, Watch
from tests.test_portfolio import book
from tools import triage

BODY = book(9, include_trades=True)


def crash_on_poison(w, stream, mid, fields):
    if fields.get(b"job") == b"poison-job":
        raise Killed


def test_poisoned_messages_do_not_stop_the_book(siege, r, make_client):
    baseline = siege.clean_book(BODY)
    taskqueue.ensure_groups(r)
    watch = Watch(("worker_tasks_total", {"kind": "unknown", "outcome": "malformed"}),
                  ("worker_tasks_total", {"kind": "portfolio", "outcome": "poison"}),
                  ("worker_tasks_total", {"kind": "bogus", "outcome": "error"}))
    fault = siege.fault("malformed message, a crash-looping task and an unpriceable task in the stream",
                        "malformed dropped and acked; crash-looper failed after MAX_DELIVERIES; "
                        "the book still prices; nothing left pending", "PoisonTrade")
    r.xadd(taskqueue.INTRADAY, {"junk": "added by hand"})
    taskqueue.submit(r, "portfolio", "intraday", [{"trades": []}], {"book_id": "POISON"}, job_id="poison-job")
    taskqueue.submit(r, "bogus", "intraday", [{}], {"book_id": "BOGUS"}, job_id="bogus-job")
    pool = siege.pool(r)
    pool.start("w1", hook=crash_on_poison, supervised=True)
    pool.start("w2", hook=crash_on_poison, supervised=True)
    with make_client(cache=PriceCache(r), queue=r) as c:
        job = c.post("/portfolio/price", json=BODY, headers={"Idempotency-Key": "siege-poison",
                                                              "Prefer": "respond-async"}).json()["job_id"]
        got = siege.readback_book(c, job)
        settled = h.wait_for(lambda: taskqueue.done(r, "poison-job") == 1 and taskqueue.done(r, "bogus-job") == 1
                             and not sum(b["lag"] + b["pending"] for b in taskqueue.backlog(r).values()))
    poison = taskqueue.results(r, "poison-job").get(0, {})
    bogus = taskqueue.results(r, "bogus-job").get(0, {})
    # What the triage tool would tell the on-call about the same failed job.
    finding = triage.analyze({"health": {}, "metrics": {}, "jobs": [
        {"kind": "portfolio", "job_id": "poison-job", "status": "failed", "n_failed": 1,
         "detail": poison.get("error", "")}]})
    fault.finish(watch, f"{pool.deaths} worker deaths, poison: {poison.get('error')!r}, "
                 f"bogus: {bogus.get('error')!r}, book diffs: {len(h.diff(baseline, got))}",
                 book_identical_to_baseline=got is not None and not h.diff(baseline, got),
                 settled_nothing_hangs=settled,
                 poison_failed_after_max_deliveries=pool.deaths == taskqueue.MAX_DELIVERIES
                 and poison.get("error") == "exceeded max deliveries",
                 bad_kind_failed_not_crashed="unknown task kind" in bogus.get("error", ""),
                 all_three_signals_moved=len(watch.moved()) == 3,
                 triage_routes_to_quant=any(f.owner == triage.OWNERS["quant"] for f in finding))
