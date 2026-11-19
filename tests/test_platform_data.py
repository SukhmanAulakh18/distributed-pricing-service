"""docs/data/platform.json feeds the platform page. It must equal a fresh build
from bench/results, and the page's no-script numbers must match it."""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import build_platform_data  # noqa: E402

DATA = os.path.join(ROOT, "docs", "data", "platform.json")
PAGE = os.path.join(ROOT, "docs", "platform.html")


def test_platform_json_is_current():
    with open(DATA) as f:
        assert f.read() == build_platform_data.render()


def test_build_is_deterministic():
    assert build_platform_data.render() == build_platform_data.render()


def test_proof_numbers_are_the_measured_ones():
    d = json.load(open(DATA))
    p = d["proof"]
    assert p["identical_workers"] == [1, 2, 4, 8]
    assert p["trades_failed"] == 0
    assert p["siege_passed"] == p["siege_total"] == 6
    assert p["cassandra_kill_errors"] == 0 and p["cassandra_kill_requests"] > 0
    assert d["chaos"]["summary"]["price_errors"] == len(d["chaos"]["errors"])
    assert sum(b[1] for b in d["chaos"]["line"]) == d["chaos"]["summary"]["price_requests"]


def test_page_fallbacks_match_the_data():
    d = json.load(open(DATA))
    html = open(PAGE).read()
    assert f'{d["proof"]["bs_per_s"] / 1e6:.0f}M' in html
    assert f'{d["proof"]["availability_pct"]:.3f}%' in html
    assert "data/platform.json" in html
    for w in d["chaos"]["windows"]:
        if w["fault"] == "cassandra node":
            assert f'{w["requests"]:,}' in html and f'{w["p99_ms"]:.0f} ms' in html
    for s in d["siege"]:
        assert s["alert"] in html
