"""The portal page only looks numbers up. These tests keep the lookup table
honest: complete, identical to the benchmark result, and reproducible from the
real /risk/predeal endpoint."""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "bench"))
import bench_portal  # noqa: E402

PAGES = os.path.join(ROOT, "docs", "data", "portal.json")
BENCH = os.path.join(ROOT, "bench", "results", "portal.json")


def load(p):
    with open(p) as f:
        return json.load(f)


def test_grid_is_complete():
    d = load(PAGES)
    want = {f"{n}|{t}|{c}" for n in bench_portal.NOTIONALS for t in bench_portal.TENORS for c in ("on", "off")}
    assert set(d["cells"]) == want
    assert d["synthetic"] is True and d["currency"] == "CAD"
    for cell in d["cells"].values():
        assert cell["verdict"] in {"approve", "approve_with_csa", "refer_to_credit"}
        assert cell["uncollateralized_running_bps"] >= 0 and cell["cva_charge_running_bps"] >= 0


def test_pages_copy_equals_bench_result():
    assert load(PAGES) == load(BENCH)


def test_one_cell_reruns_identically(make_client):
    d = load(PAGES)
    n, t = 5_000_000, 5.0
    with make_client() as c:
        for csa in (False, True):
            got = bench_portal.cell(bench_portal.quote(c, n, t, csa), n, t)
            assert got == d["cells"][f"{n}|{t}|{'on' if csa else 'off'}"]


def test_csa_never_costs_more_than_no_csa():
    d = load(PAGES)
    for n in bench_portal.NOTIONALS:
        for t in bench_portal.TENORS:
            assert d["cells"][f"{n}|{t}|on"]["cva_charge_running_bps"] <= d["cells"][f"{n}|{t}|off"]["uncollateralized_running_bps"]
