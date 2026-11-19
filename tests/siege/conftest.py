import os

import pytest

from service import taskqueue, worker
from tests.siege import harness


@pytest.fixture(scope="session", autouse=True)
def scorecard():
    yield
    # Only `make siege` refreshes the committed scorecard; ordinary test runs
    # (and CI) leave the working tree clean.
    if os.getenv("SIEGE_SCORECARD"):
        harness.write_scorecard(harness.ROOT / "bench" / "results" / "siege.json")


@pytest.fixture
def siege(request, make_client, monkeypatch):
    # Short claim and reclaim clocks so a "dead" worker is noticed in
    # milliseconds, not the production 10 s.
    monkeypatch.setattr(taskqueue, "CLAIM_IDLE_MS", 150)
    monkeypatch.setattr(worker, "RECLAIM_EVERY_S", 0.05)
    cleanup: list = []
    yield harness.Siege(request.node.name.removeprefix("test_"), make_client, cleanup)
    for fn in cleanup:
        fn()
