"""Build docs/data/kit.json for the pitch kit's guided walkthrough.

Reads the three measured result files and keeps only the fields the page
uses. Deterministic: sorted keys, no timestamps. Run with --check to fail if
the committed file is stale (tests/test_kit_data.py does the same)."""
import argparse
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
XVA = os.path.join(ROOT, "bench", "results", "xva.json")
SIEGE = os.path.join(ROOT, "bench", "results", "siege.json")
PORTAL = os.path.join(ROOT, "docs", "data", "portal.json")
OUT = os.path.join(ROOT, "docs", "data", "kit.json")

TENORS = [1.0, 2.0, 5.0]
FAULTS = {
    "worker_killed": "worker_killed_mid_task",
    "redis_outage": "redis_outage_mid_book",
    "client_reset": "client_reset_then_retry_with_same_key",
}


def load(path):
    with open(path) as f:
        return json.load(f)


def build():
    xva, siege, portal = load(XVA), load(SIEGE), load(PORTAL)
    cells = {}
    for n in portal["notionals_usd"]:
        for t in TENORS:
            off = portal["cells"][f"{n}|{t}|off"]
            on = portal["cells"][f"{n}|{t}|on"]
            cells[f"{n}|{t}"] = {
                "forward_rate": off["forward_rate"],
                "off_bps": off["uncollateralized_running_bps"],
                "on_bps": on["cva_charge_running_bps"],
                "peak_pfe": off["peak_pfe_after"],
                "utilisation": off["limit_utilisation"],
                "verdict": off["verdict"],
            }
    by_name = {s["scenario"]: s for s in siege["scenarios"]}
    faults = {}
    for key, name in FAULTS.items():
        s = by_name[name]
        assert s["passed"], name
        faults[key] = {
            "alert": s["alert"],
            "expected": s["expected"],
            "metric": s["detected_by"][0].split("{")[0],
            "owner": s["routed_to"],
            "recovered_s": s["recovered_s"],
        }
    p = xva["predeal_endpoint"]
    return {
        "cells": cells,
        "faults": faults,
        "limit_cad": portal["inputs"]["credit_limit_cad_on_peak_pfe"],
        "notionals": portal["notionals_usd"],
        "predeal": {
            "first_quote_ms": p["first_quote_ms"],
            "requote_ms": p["requote_book_cached_ms"],
            "threads": p["pricer_threads"],
            "trades": p["n_trades"],
        },
        "siege": {"passed": siege["passed"], "total": siege["passed"] + siege["failed"]},
        "tenors": TENORS,
    }


def render():
    return json.dumps(build(), indent=1, sort_keys=True) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if docs/data/kit.json is stale")
    args = ap.parse_args(argv)
    text = render()
    if args.check:
        with open(OUT) as f:
            if f.read() != text:
                print("docs/data/kit.json is stale; run scripts/build_kit_data.py", file=sys.stderr)
                return 1
        return 0
    with open(OUT, "w") as f:
        f.write(text)
    print(f"wrote {os.path.relpath(OUT, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
