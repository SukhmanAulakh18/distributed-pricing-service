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
    seed_src = json.dumps(req.model_dump(exclude={"block_size"}), sort_keys=True).encode()
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
        "grid": req.grid,
        "rate": req.rate,
        # Same request -> same seed -> same numbers, wherever it runs.
        "seed": int(hashlib.sha256(seed_src).hexdigest()[:16], 16),
        "tail_k": req.tail_k,
    }
    tasks = [{"start": s, "count": min(req.block_size, req.n_scenarios - s)}
             for s in range(0, req.n_scenarios, req.block_size)]
    return payload, tasks


def run_block(payload: dict, start: int, count: int, threads: int) -> tuple[dict, bytes]:
    """Runs one scenario block in C++. Returns small JSON-able sums and the
    PFE tail as raw float64 bytes (binary is 3x smaller than JSON numbers)."""
    f64 = lambda k: np.asarray(payload[k], dtype=np.float64)  # noqa: E731
    i32 = lambda k: np.asarray(payload[k], dtype=np.int32)  # noqa: E731
    n_u = len(payload["spot"])
    out = pricer_core.simulate_exposure(
        f64("spot"), f64("vol"), f64("div"), f64("corr").reshape(n_u, n_u), i32("u_idx"), i32("kind"),
        f64("strike"), f64("maturity"), f64("qty"), f64("grid"), payload["rate"], payload["seed"],
        start, count, payload["tail_k"], threads)
    meta = {"sum": out["sum"].tolist(), "sum_sq": out["sum_sq"].tolist(), "n": int(out["n"]),
            "k": int(out["tails"].shape[1])}
    return meta, out["tails"].astype("<f8").tobytes()


def merge(blocks: list[tuple[dict, bytes]], grid: list[float], tail_k: int) -> dict:
    """Combines blocks exactly. EE: add sums. PFE: the global top k at each
    step is inside the union of the blocks' top k, so take the k-th largest of
    the union. Gives the same answer as a single run, to the last bit for PFE."""
    steps = len(grid)
    total = np.zeros(steps)
    n = 0
    tails = []
    for meta, blob in blocks:
        total += np.asarray(meta["sum"])
        n += meta["n"]
        tails.append(np.frombuffer(blob, dtype="<f8").reshape(steps, meta["k"]))
    union = np.concatenate(tails, axis=1)
    k = min(tail_k, union.shape[1])
    pfe = -np.sort(-union, axis=1)[:, k - 1]  # k-th largest per step
    return {"ee": (total / n).tolist(), "pfe": pfe.tolist(), "n_scenarios": n}


def cva(grid: list[float], ee: list[float], rate: float, recovery: float, spread_bps: float) -> float:
    """Unilateral CVA = (1 - R) * sum_i DF(t_i) EE(t_i) [Q(t_{i-1}) - Q(t_i)],
    with a flat hazard rate from the credit-triangle approximation
    lambda = s / (1 - R) and survival Q(t) = exp(-lambda t)."""
    lam = spread_bps / 1e4 / (1 - recovery)
    total, prev_t = 0.0, 0.0
    for t, e in zip(grid, ee):
        default_prob = math.exp(-lam * prev_t) - math.exp(-lam * t)
        total += math.exp(-rate * t) * e * default_prob
        prev_t = t
    return (1 - recovery) * total


def epe(grid: list[float], ee: list[float]) -> float:
    """Time-averaged expected exposure over the horizon."""
    prev_t, area = 0.0, 0.0
    for t, e in zip(grid, ee):
        area += e * (t - prev_t)
        prev_t = t
    return area / grid[-1]
