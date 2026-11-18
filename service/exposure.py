import hashlib
import json
import math

import numpy as np
import pricer_core

from .models import ExposureRequest

_KIND = {"call": 0, "put": 1, "forward": 2}


def plan(req: ExposureRequest) -> tuple[dict, list[dict]]:
    """Splits a request into a shared payload (stored once) and per-block
    tasks that reference it. Every block needs the whole netting set, so
    copying it into each queue message would multiply Redis traffic by the
    number of blocks."""
    names = [u.name for u in req.underlyings]
    idx = {n: i for i, n in enumerate(names)}
    corr = req.correlation or np.eye(len(names)).tolist()
    nodes, lag, report = req.sim_grid
    # The seed covers the market state and the simulated nodes only, not the
    # trades, credit or CSA amounts. Adding a trade, changing a spread or a
    # threshold then reuses the same scenarios (common random numbers), so a
    # pre-deal incremental CVA or a sensitivity is a clean difference rather
    # than two independent noisy estimates. Changing the MPoR (or switching
    # the CSA on) adds nodes and so changes the paths; compare against the
    # uncollateralized figures the same run reports instead.
    seed_src = json.dumps({"md": req.market_data_version, "rate": req.rate, "corr": corr, "grid": nodes,
                           "underlyings": [u.model_dump() for u in req.underlyings]}, sort_keys=True).encode()
    payload = {
        "spot": [u.spot for u in req.underlyings],
        "vol": [u.vol for u in req.underlyings],
        "div": [u.div_yield for u in req.underlyings],
        "corr": [x for row in corr for x in row],
        "u_idx": [idx[t.underlying] for t in req.trades],
        "kind": [_KIND[t.kind] for t in req.trades],
        "strike": [t.strike for t in req.trades],
        "maturity": [t.maturity for t in req.trades],
        "qty": [t.quantity for t in req.trades],
        "grid": nodes,
        "report": report,
        "rate": req.rate,
        # Same market state -> same seed -> same numbers, wherever it runs.
        "seed": int(hashlib.sha256(seed_src).hexdigest()[:16], 16),
        "tail_k": req.tail_k,
        "csa": None if req.csa is None else {
            "threshold_recv": req.csa.threshold_recv, "threshold_post": req.csa.threshold_post,
            "mta": req.csa.mta, "independent_amount": req.csa.independent_amount, "lag_index": lag},
    }
    tasks = [{"start": s, "count": min(req.block_size, req.n_scenarios - s)}
             for s in range(0, req.n_scenarios, req.block_size)]
    return payload, tasks


def run_block(payload: dict, start: int, count: int, threads: int) -> tuple[dict, bytes]:
    """Runs one scenario block in C++. Returns small JSON-able sums and the
    PFE tails as raw float64 bytes (binary is 3x smaller than JSON numbers):
    the uncollateralized tail, then the collateralized one if there is a CSA."""
    f64 = lambda k: np.asarray(payload[k], dtype=np.float64)  # noqa: E731
    i32 = lambda k: np.asarray(payload[k], dtype=np.int32)  # noqa: E731
    n_u = len(payload["spot"])
    out = pricer_core.simulate_exposure(
        f64("spot"), f64("vol"), f64("div"), f64("corr").reshape(n_u, n_u), i32("u_idx"), i32("kind"),
        f64("strike"), f64("maturity"), f64("qty"), f64("grid"), payload["rate"], payload["seed"],
        start, count, payload["tail_k"], threads, csa=payload.get("csa"))
    meta = {"n": int(out["n"]), "k": int(out["tails"].shape[1]), "v0": float(out["v0"])}
    series = ["", "csa_"] if "csa_sum" in out else [""]
    for p in series:
        for f in ("sum", "sum_sq", "neg_sum"):
            meta[p + f] = out[p + f].tolist()
    blob = b"".join(out[p + "tails"].astype("<f8").tobytes() for p in series)
    return meta, blob


def merge(blocks: list[tuple[dict, bytes]], grid: list[float], tail_k: int,
          report: list[int] | None = None) -> dict:
    """Combines blocks exactly. EE: add sums. PFE: the global top k at each
    step is inside the union of the blocks' top k, so take the k-th largest of
    the union. Gives the same answer as a single run, to the last bit for PFE.
    `report` picks the reporting dates out of the simulated nodes."""
    steps = len(grid)
    report = list(range(steps)) if report is None else report
    has_csa = {"csa_sum" in meta for meta, _ in blocks}
    if len(has_csa) > 1:
        # Mid rollout, an old worker drops the CSA series: fail loudly rather
        # than return a result that silently lacks it.
        raise ValueError("blocks disagree on CSA series (mixed worker versions); resubmit the run")
    series = ["", "csa_"] if has_csa.pop() else [""]
    n = sum(meta["n"] for meta, _ in blocks)
    out = {"n_scenarios": n, "v0": blocks[0][0].get("v0", 0.0)}
    for i, p in enumerate(series):
        s1 = sum(np.asarray(meta[p + "sum"]) for meta, _ in blocks)
        s2 = sum(np.asarray(meta[p + "sum_sq"]) for meta, _ in blocks)
        neg = sum(np.asarray(meta.get(p + "neg_sum", np.zeros(steps))) for meta, _ in blocks)
        tails = [np.frombuffer(blob, dtype="<f8").reshape(len(series), steps, meta["k"])[i] for meta, blob in blocks]
        union = np.concatenate(tails, axis=1)
        k = min(tail_k, union.shape[1])
        # k-th largest per step; partition is O(n) where a full sort is O(n log n).
        pfe = -np.partition(-union, k - 1, axis=1)[:, k - 1]
        ee = s1 / n
        se = np.sqrt(np.maximum(s2 / n - ee * ee, 0.0) / n)
        name = "" if not p else "_csa"
        out["ee" + name] = ee[report].tolist()
        out["ee_se" + name] = se[report].tolist()
        out["ene" + name] = (neg / n)[report].tolist()
        out["pfe" + name] = pfe[report].tolist()
    return out


def _survival(spread_bps: float, recovery: float, t: float) -> float:
    # Flat hazard from the credit triangle: lambda = s / (1 - R).
    return math.exp(-spread_bps / 1e4 / (1 - recovery) * t)


def cva(grid: list[float], ee: list[float], rate: float, recovery: float, spread_bps: float,
        ee0: float = 0.0, own: tuple[float, float] | None = None) -> float:
    """CVA = (1 - R) * sum_i DF*EE(mid of (t_{i-1}, t_i]) [Q(t_{i-1}) - Q(t_i)].

    DF*EE over each interval is the average of its two ends (trapezoid), with
    EE(0) = max(V0, 0): pairing the interval's default probability with only
    its right end overstates CVA for profiles that fall over time. `own` =
    (recovery, spread_bps) of our own credit makes it bilateral: the
    counterparty's default only costs us if we have not defaulted first."""
    total, prev_t, prev_de = 0.0, 0.0, ee0
    for t, e in zip(grid, ee):
        de = math.exp(-rate * t) * e
        pd = _survival(spread_bps, recovery, prev_t) - _survival(spread_bps, recovery, t)
        alive = _survival(own[1], own[0], prev_t) if own else 1.0
        total += 0.5 * (prev_de + de) * pd * alive
        prev_t, prev_de = t, de
    return (1 - recovery) * total


def epe(grid: list[float], ee: list[float], ee0: float = 0.0) -> float:
    """Time-averaged expected exposure over the horizon (trapezoid from t = 0)."""
    prev_t, prev_e, area = 0.0, ee0, 0.0
    for t, e in zip(grid, ee):
        area += 0.5 * (prev_e + e) * (t - prev_t)
        prev_t, prev_e = t, e
    return area / grid[-1]
