"""Builds the project report from the measured results.

Every number on the page is read from bench/results/*.json or the chaos-run
logs, so the report cannot drift from the measurements. Writes:
  docs/report.html  page body (published as a shareable page)
  docs/index.html   the same page as a full document (GitHub Pages)

Run: python scripts/build_report.py
"""
import json
import os
import re
from string import Template

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RES = os.path.join(ROOT, "bench", "results")
REPO = "https://github.com/SukhmanAulakh18/distributed-pricing-service"


def load(name):
    with open(os.path.join(RES, name)) as f:
        return json.load(f)


def chaos_summary(log):
    text = open(os.path.join(RES, log)).read()
    return json.loads(re.search(r"^\{.*?^\}", text, re.S | re.M).group(0))


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None


def chart_data(keda):
    """Small, pre-bucketed series for the interactive charts on the page."""
    c = load("chaos-run3.json")
    end = c["summary"]["minutes"] * 60
    buckets = []
    for b in range(0, int(end), 5):
        rows = [r for r in c["prices"] if b <= r[0] < b + 5]
        lat = [r[2] for r in rows if r[1]]
        buckets.append([b, sum(1 for r in rows if r[1]), sum(1 for r in rows if not r[1]),
                        pct(lat, 0.5) and round(pct(lat, 0.5), 1), pct(lat, 0.99) and round(pct(lat, 0.99), 1)])
    books = [[round(b["t"], 1), round(b["s"], 1),
              "ok" if b["status"] == 200 and b["attempts"] == 1 else "retry" if b["status"] == 200 else "lost"]
             for b in c["books"]]
    faults = [[round(f["t"], 1), f["fault"]] for f in c["faults"]]

    def series(run):
        pts = [[p[0], p[1]] for p in run["timeline"]]
        pts[-1].append(round(run["total_s"], 1))
        return pts
    return json.dumps({"chaos": {"end": end, "buckets": buckets, "books": books, "faults": faults},
                       "keda": {"cold": series(keda["cold"]), "warm": series(keda["warm"]),
                                "tmax": max(keda["cold"]["total_s"], keda["warm"]["total_s"])}},
                      separators=(",", ":"))


def findings():
    text = open(os.path.join(ROOT, "docs", "PERFORMANCE_REVIEW.md")).read()
    items = []
    for n, title in re.findall(r"^## (\d+)\. (.+)$", text, re.M):
        slug = re.sub(r"[^a-z0-9 -]", "", f"{n}. {title}".lower()).replace(" ", "-")
        items.append(f'<li><a href="{REPO}/blob/main/docs/PERFORMANCE_REVIEW.md#{slug}">{title}</a></li>')
    return "".join(items)


def fmt(x, d=0):
    return f"{x:,.{d}f}"


def numbers():
    core, port, pri, exp = load("core.json"), load("portfolio.json"), load("priority.json"), load("exposure.json")
    keda = load("keda.json")
    tracing = load("tracing.json")
    c1, c3 = chaos_summary("chaos-run1.log"), chaos_summary("chaos-run3.log")
    sc = {(r["workers"], r["book"]): r for r in port["scaling"]}
    pri = {r["mode"]: r for r in pri}
    gil = {(r["gil"], r["python_threads"]): r["calls_per_s"] for r in core["gil"]}
    batch = {r["method"]: r["options_per_s"] for r in core["batch"]}
    vr = core["variance_reduction"][-1]
    e8 = next(r for r in exp["scaling"] if r["workers"] == 8)
    return {
        "c3_avail": c3["availability_pct"], "c3_requests": fmt(c3["price_requests"]), "c3_errors": fmt(c3["price_errors"]),
        "c3_p50": c3["p50_ms"], "c3_p99": c3["p99_ms"], "c3_faults": c3["faults"],
        "c3_books": c3["books"], "c3_books_ok": c3["books_complete"], "c3_retried": c3.get("books_retried", 0),
        "c3_books_lost": c3["books"] - c3["books_complete"],
        "c3_trades": fmt(c3["trades_priced"]), "c3_trades_failed": c3["trades_failed"],
        "c3_exp": c3["exposure_runs"], "c3_exp_ok": c3["exposure_complete"],
        "c3_identical": "bit-identical" if c3["exposure_identical"] else "NOT identical",
        "c3_alerts": "".join(f"<li><code>{a.split(' -> ')[0]}</code> → {a.split(' -> ')[1]}</li>" for a in c3["alerts_fired"]),
        "c1_avail": c1["availability_pct"], "c1_requests": fmt(c1["price_requests"]), "c1_errors": c1["price_errors"],
        "c1_books": c1["books"], "c1_books_ok": c1["books_complete"],
        "w1": fmt(sc[(1, 10000)]["trades_per_s"]), "w8": fmt(sc[(8, 10000)]["trades_per_s"]),
        "b50k_s": f"{sc[(8, 50000)]['wall_s']:.1f}",
        "fifo_busy": f"{pri['fifo']['intraday_during_batch_s']:.2f}", "pri_busy": f"{pri['priority']['intraday_during_batch_s']:.2f}",
        "fifo_idle": f"{pri['fifo']['intraday_idle_s']:.2f}",
        "revals": fmt(exp["revaluations"] / 1e6), "e8_s": f"{e8['wall_s']:.1f}", "e8_rate": fmt(e8["revals_per_s"] / 1e6),
        "keda_cold": f"{keda['cold']['first_ready_s']:.0f}", "keda_cold_total": f"{keda['cold']['total_s']:.0f}",
        "keda_warm_total": f"{keda['warm']['total_s']:.0f}",
        "keda_peak": max(n for _, n in keda["cold"]["timeline"]),
        "trace_spans": tracing["book_trace"]["spans"], "trace_pods": tracing["book_trace"]["pods"],
        "trace_trades": fmt(tracing["book_trace"]["trades"]),
        "trace_off": fmt(tracing["overhead"]["off_rps"]), "trace_on": fmt(tracing["overhead"]["on_rps"]),
        "trace_cost": f"{tracing['overhead']['cost_pct']:.0f}",
        "gil_rel": f"{gil[('released', 8)]:.0f}", "gil_held": f"{gil[('held', 8)]:.0f}",
        "batch8": f"{batch['bs_batch, 8 threads'] / 1e6:.0f}", "percall": f"{batch['per-call C++ binding'] / 1e6:.1f}",
        "cv": f"{vr['variance_reduction']:,.0f}", "cv_paths": fmt(vr["paths_to_match_plain"]),
        "repo": REPO,
        "chart_data": chart_data(keda), "findings": findings(),
    }


PAGE = Template(open(os.path.join(ROOT, "docs", "report.template.html")).read())


def main():
    body = PAGE.substitute(numbers())
    with open(os.path.join(ROOT, "docs", "report.html"), "w") as f:
        f.write(body)
    with open(os.path.join(ROOT, "docs", "index.html"), "w") as f:
        f.write('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                "</head><body>\n" + body + "\n</body></html>\n")
    print("wrote docs/report.html and docs/index.html")


if __name__ == "__main__":
    main()
