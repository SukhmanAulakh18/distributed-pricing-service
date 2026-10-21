"""Synthetic books for the benchmarks. Deterministic for a given seed."""
import random

UNDERLYINGS = ["SPX", "TSX", "AAPL", "MSFT", "RY", "TD", "BNS", "SHOP"]


def book(n: int, seed: int = 0, mix=(0.4, 0.4, 0.2)) -> list[dict]:
    """n trades: a share of analytic Europeans (e-trading quotes), Monte Carlo
    Europeans and Monte Carlo Asians. Strikes and maturities are drawn so
    almost every trade is a distinct contract (no cache sharing)."""
    rng = random.Random(seed)
    trades = []
    for i in range(n):
        u = rng.random()
        kind = "analytic" if u < mix[0] else "mc" if u < mix[0] + mix[1] else "asian"
        product = {"type": "asian" if kind == "asian" else "european", "spot": 100.0,
                   "strike": round(rng.uniform(70, 130), 4), "rate": 0.03,
                   "vol": round(rng.uniform(0.1, 0.6), 4), "maturity": round(rng.uniform(0.1, 3.0), 4),
                   "is_call": rng.random() < 0.5}
        if kind == "asian":
            product["n_fixings"] = 12
        elif kind == "analytic":
            product["method"] = "analytic"
        trades.append({"trade_id": f"T{i:06d}", "underlying": rng.choice(UNDERLYINGS),
                       "quantity": rng.choice([-1, 1]) * rng.randint(1, 100), "product": product})
    return trades
