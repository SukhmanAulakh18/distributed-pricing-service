"""Charts for the README from bench/results/*.json -> docs/img/*.png.

Run: python bench/plots.py
"""
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = os.path.dirname(__file__)
RES = os.path.join(HERE, "results")
OUT = os.path.join(HERE, "..", "docs", "img")

# Validated categorical slots 1-3 (light mode) and text/surface tokens.
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "font.size": 10, "axes.titlesize": 12, "axes.titleweight": "bold",
    "axes.titlelocation": "left", "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False, "lines.linewidth": 2, "lines.markersize": 6,
    "legend.frameon": False, "axes.axisbelow": True,
})


def load(name):
    with open(os.path.join(RES, name)) as f:
        return json.load(f)


def save(fig, name):
    os.makedirs(OUT, exist_ok=True)
    fig.tight_layout()
    fig.savefig(os.path.join(OUT, name), dpi=150)
    plt.close(fig)


def label_end(ax, x, y, text, color):
    ax.annotate(text, (x, y), xytext=(6, 0), textcoords="offset points", va="center", color=INK2, fontsize=9)


def hbars(ax, labels, values, fmt, color=BLUE):
    ys = range(len(labels))
    ax.barh(ys, values, color=color, height=0.6, edgecolor=SURFACE, linewidth=2)
    ax.set_yticks(list(ys), labels)
    ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    for y, v in zip(ys, values):
        ax.annotate(fmt(v), (v, y), xytext=(4, 0), textcoords="offset points", va="center", color=INK, fontsize=9)


def core():
    c = load("core.json")
    fig, ax = plt.subplots(figsize=(6.4, 3.8))
    for anti, color, name in ((False, ORANGE, "plain"), (True, BLUE, "antithetic")):
        rows = [r for r in c["convergence"] if r["antithetic"] == anti]
        xs, ys = [r["paths"] for r in rows], [r["std_err"] for r in rows]
        ax.plot(xs, ys, "o-", color=color, label=name)
        label_end(ax, xs[-1], ys[-1], name, color)
    first = [r for r in c["convergence"] if not r["antithetic"]][0]
    xs = [r["paths"] for r in c["convergence"] if not r["antithetic"]]
    ax.plot(xs, [first["std_err"] * (xs[0] / x) ** 0.5 for x in xs], "--", color=INK2, lw=1, label="1/√N")
    ax.set(xscale="log", yscale="log", xlabel="paths", ylabel="standard error",
           title="Monte Carlo error falls as 1/√N")
    ax.legend(loc="lower left")
    save(fig, "convergence.png")

    fig, ax = plt.subplots(figsize=(6.4, 2.4))
    vr = c["variance_reduction"]
    hbars(ax, [r["setting"] for r in vr], [r["paths_to_match_plain"] for r in vr], lambda v: f"{v:,.0f}")
    ax.set(xlabel="paths needed to match plain Monte Carlo at 200,000", xlim=(0, max(r["paths_to_match_plain"] for r in vr) * 1.18),
           title="Asian option: control variate needs 1,000x fewer paths")
    save(fig, "variance_reduction.png")

    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    for gil, color in (("released", BLUE), ("held", ORANGE)):
        rows = [r for r in c["gil"] if r["gil"] == gil]
        xs, ys = [r["python_threads"] for r in rows], [r["calls_per_s"] for r in rows]
        ax.plot(xs, ys, "o-", color=color, label=f"GIL {gil}")
        label_end(ax, xs[-1], ys[-1], f"GIL {gil}", color)
    ax.set(xlabel="Python threads calling the pricer", ylabel="pricings per second", xticks=[1, 2, 4, 8],
           ylim=(0, None), title="Releasing the GIL lets Python threads price in parallel")
    ax.legend(loc="upper left")
    save(fig, "gil.png")

    fig, ax = plt.subplots(figsize=(6.4, 2.6))
    b = c["batch"]
    hbars(ax, [r["method"] for r in b], [r["options_per_s"] for r in b], lambda v: f"{v / 1e6:,.1f}M/s")
    ax.set(xlabel="Black-Scholes prices per second", xlim=(0, max(r["options_per_s"] for r in b) * 1.2),
           title="Batched, zero-copy pricing vs one call per option")
    ax.xaxis.set_major_formatter(lambda v, _: f"{v / 1e6:.0f}M")
    save(fig, "batch.png")


def distributed():
    p, e = load("portfolio.json"), load("exposure.json")
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.6, 3.6))
    for book, color in ((10_000, BLUE), (50_000, ORANGE)):
        rows = [r for r in p["scaling"] if r["book"] == book]
        xs, ys = [r["workers"] for r in rows], [r["trades_per_s"] for r in rows]
        a1.plot(xs, ys, "o-", color=color, label=f"{book:,}-trade book")
    a1.set(xlabel="workers", ylabel="trades priced per second", xticks=[1, 2, 4, 8], ylim=(0, None),
           title="Book pricing (mixed book)")
    a1.legend(loc="lower right")
    rows = e["scaling"]
    xs, ys = [r["workers"] for r in rows], [r["revals_per_s"] / 1e6 for r in rows]
    a2.plot(xs, ys, "o-", color=AQUA)
    for x, y in zip(xs, ys):
        a2.annotate(f"{y:.0f}M", (x, y), xytext=(0, 7), textcoords="offset points", ha="center", color=INK, fontsize=9)
    a2.set(xlabel="workers", ylabel="option revaluations per second (M)", xticks=[1, 2, 4, 8],
           ylim=(0, max(ys) * 1.18), title="Exposure run (same answer at any count)")
    save(fig, "scaling.png")

    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ts = p["task_size"]
    xs, ys = [r["task_size"] for r in ts], [r["trades_per_s"] for r in ts]
    ax.plot(xs, ys, "o-", color=BLUE)
    for x, y in zip(xs, ys):
        ax.annotate(f"{y:,.0f}", (x, y), xytext=(0, 7), textcoords="offset points", ha="center", color=INK, fontsize=9)
    ax.set(xscale="log", xlabel="trades per queue message (10,000-trade book, 4 workers)",
           ylabel="trades per second", ylim=(0, max(ys) * 1.2),
           title=f"Task size: one trade per message is {max(ys) / ys[0]:.1f}x slower than the best")
    save(fig, "task_size.png")

    pr = load("priority.json")
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    modes = [r["mode"] for r in pr]
    names = {"fifo": "one FIFO queue", "priority": "intraday ahead of batch"}
    w = 0.36
    for i, (key, color, label) in enumerate((("intraday_idle_s", BLUE, "system idle"),
                                             ("intraday_during_batch_s", ORANGE, "during a batch exposure run"))):
        xs = [m + (i - 0.5) * w for m in range(len(modes))]
        vals = [r[key] for r in pr]
        ax.bar(xs, vals, width=w, color=color, edgecolor=SURFACE, linewidth=2, label=label)
        for x, v in zip(xs, vals):
            ax.annotate(f"{v:.2f}s", (x, v), xytext=(0, 3), textcoords="offset points", ha="center", color=INK, fontsize=9)
    ax.set_xticks(range(len(modes)), [names[m] for m in modes])
    ax.grid(axis="x", visible=False)
    ax.set(ylabel="time to price a 1,000-trade book (s)", title="Batch work no longer delays intraday pricing")
    ax.legend(loc="upper right")
    save(fig, "priority.png")

    prof = e["profile"]
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    ax.plot(prof["grid"], prof["ee"], color=BLUE, label="expected exposure (EE)")
    ax.plot(prof["grid"], prof["pfe"], color=ORANGE, label="potential future exposure (PFE 95%)")
    ax.set(xlabel="years", ylabel="exposure", ylim=(0, None),
           title=f"Netting set exposure profile, {prof['n_scenarios']:,} scenarios")
    ax.legend(loc="upper right")
    save(fig, "exposure_profile.png")


def chaos(name="chaos-run3.json"):
    path = os.path.join(RES, name)
    if not os.path.exists(path):
        return
    c = load(name)
    end = c["summary"]["minutes"] * 60
    ok, err = {}, {}
    for t, good, _ in c["prices"]:
        b = int(t // 5) * 5
        (ok if good else err)[b] = (ok if good else err).get(b, 0) + 1
    xs = list(range(0, int(end), 5))
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 4.6), sharex=True, gridspec_kw={"height_ratios": [3, 1.2]})
    a1.plot(xs, [ok.get(x, 0) / 5 for x in xs], color=BLUE, lw=1.6)
    a1.set(ylabel="prices served / s", ylim=(0, None),
           title=f"10 minutes, {len(c['faults'])} faults: {c['summary']['availability_pct']}% of single prices served, "
                 f"{c['summary']['books_complete']} of {c['summary']['books']} books complete")
    a2.bar(xs, [err.get(x, 0) for x in xs], width=4, color=ORANGE, edgecolor=SURFACE, linewidth=1)
    a2.set(ylabel="errors / 5 s", xlabel="seconds into the run", ylim=(0, max([3] + list(err.values())) * 1.2))
    top = a1.get_ylim()[1]
    for f in c["faults"]:
        for ax in (a1, a2):
            ax.axvline(f["t"], color=INK2, lw=0.8, ls="--")
        a1.annotate(f["fault"], (f["t"], top), xytext=(2, -2), textcoords="offset points", rotation=90,
                    va="top", ha="left", fontsize=7.5, color=INK2)
    save(fig, "chaos_timeline.png")


if __name__ == "__main__":
    core()
    distributed()
    chaos()
    print("charts written to", os.path.abspath(OUT))
