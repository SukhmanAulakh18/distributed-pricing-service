"""docs/data/kit.json feeds the kit's walkthrough. It must equal what the build
script produces from the measured result files, so it cannot go stale
silently, and the page's no-script fallbacks must match it."""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import build_kit_data  # noqa: E402

KIT = os.path.join(ROOT, "docs", "data", "kit.json")
PAGE = os.path.join(ROOT, "docs", "kit.html")
THEMES = os.path.join(ROOT, "docs", "themes")


def test_kit_json_is_current():
    with open(KIT) as f:
        assert f.read() == build_kit_data.render()


def test_grid_covers_every_choice():
    d = json.load(open(KIT))
    for n in d["notionals"]:
        for t in d["tenors"]:
            c = d["cells"][f"{n}|{t}"]
            assert c["on_bps"] < c["off_bps"]
    assert d["siege"] == {"passed": 6, "total": 6}


def test_page_fallbacks_match_the_data():
    """No-script visitors read static numbers; they must be the default cell's."""
    d = json.load(open(KIT))
    html = open(PAGE).read()
    c = d["cells"]["5000000|5.0"]
    assert f'{c["forward_rate"]:.4f}' in html
    assert f'{c["off_bps"]:.1f}' in html and f'{c["on_bps"]:.1f}' in html
    assert f'{c["utilisation"] * 100:.0f}%' in html
    assert f'{d["predeal"]["first_quote_ms"] / 1000:.1f} s' in html
    for f in d["faults"].values():
        assert f["expected"] in html and f["metric"] in html and f["alert"] in html


def test_page_skins_match_the_theme_files():
    html = open(PAGE).read().lower()
    for name in ("neutral", "bank-a"):
        t = json.load(open(os.path.join(THEMES, name + ".json")))
        for mode in ("light", "dark"):
            for hexv in t["colors"][mode].values():
                assert hexv.lower() in html, (name, mode, hexv)


def test_front_door_fallbacks_match_the_data():
    """The front door's no-script defaults (10M USD, 2 years) and proof strip come from kit.json."""
    d = json.load(open(KIT))
    html = open(os.path.join(ROOT, "docs", "index.html")).read()
    c = d["cells"]["10000000|2.0"]
    assert f'{c["forward_rate"]:.4f}' in html
    assert f'{c["off_bps"]:.1f}' in html and f'{c["on_bps"]:.1f}' in html
    assert f'{round((1 - c["on_bps"] / c["off_bps"]) * 100)}%' in html
    assert f'{d["proof"]["chaos_availability_pct"]:.3f}%' in html
    assert f'{d["proof"]["chaos_price_requests"]:,}' in html
    best = max(1 - x["on_bps"] / x["off_bps"] for x in d["cells"].values())
    assert f"{round(best * 100)}%" in html
