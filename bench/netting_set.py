"""Synthetic netting set for the exposure benchmarks."""
import random

NAMES = ["SPX", "TSX", "STOXX", "NKY", "FTSE"]
CORR = [[1.0, 0.7, 0.6, 0.4, 0.6],
        [0.7, 1.0, 0.5, 0.3, 0.5],
        [0.6, 0.5, 1.0, 0.4, 0.7],
        [0.4, 0.3, 0.4, 1.0, 0.4],
        [0.6, 0.5, 0.7, 0.4, 1.0]]


def netting_set(n_trades: int, n_scenarios: int, block_size: int = 5_000, horizon: float = 5.0, seed: int = 0) -> dict:
    rng = random.Random(seed)
    trades = []
    for i in range(n_trades):
        kind = rng.choice(["call", "put", "forward"])
        trades.append({"trade_id": f"X{i:05d}", "underlying": rng.choice(NAMES), "kind": kind,
                       "strike": round(rng.uniform(80, 120), 2), "maturity": round(rng.uniform(0.25, horizon), 3),
                       "quantity": rng.choice([-1, 1]) * rng.randint(1, 50)})
    return {"netting_set_id": "NS-BENCH", "market_data_version": "md-bench", "rate": 0.03,
            "underlyings": [{"name": n, "spot": 100.0, "vol": v} for n, v in zip(NAMES, [0.18, 0.2, 0.22, 0.25, 0.2])],
            "correlation": CORR, "trades": trades, "horizon_years": horizon, "step_months": 1,
            "n_scenarios": n_scenarios, "block_size": block_size, "pfe_quantile": 0.95}
