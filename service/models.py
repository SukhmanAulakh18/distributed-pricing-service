import math
from typing import Annotated, Literal, Union

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

# Work caps (one unit = one simulated time step of one path, or one trade
# revaluation). They turn an accidental 10^10-path request into a 422 instead
# of a pod that is OOM-killed or pinned at 100% CPU for an hour.
MAX_REQUEST_WORK = 500_000_000
MAX_PORTFOLIO_WORK = 50_000_000_000
MAX_EXPOSURE_WORK = 200_000_000_000
MAX_TAIL_BYTES = 64 * 1024 * 1024


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OptionTerms(Strict):
    spot: float = Field(gt=0)
    strike: float = Field(gt=0)
    rate: float = Field(ge=-0.05, le=1.0)
    div_yield: float = Field(default=0.0, ge=0, le=1.0)
    vol: float = Field(gt=0, le=5.0)
    maturity: float = Field(gt=0, le=30.0)
    is_call: bool = True


class European(OptionTerms):
    type: Literal["european"]
    # analytic: closed-form Black-Scholes in microseconds (e-trading quotes);
    # mc: Monte Carlo, the same engine the path-dependent products use.
    method: Literal["analytic", "mc"] = "mc"

    @property
    def steps(self) -> int:
        return 0 if self.method == "analytic" else 1


class Asian(OptionTerms):
    type: Literal["asian"]
    n_fixings: int = Field(default=12, ge=1, le=1000)
    control_variate: bool = True

    @property
    def steps(self) -> int:
        return self.n_fixings


Product = Annotated[Union[European, Asian], Field(discriminator="type")]


class Simulation(Strict):
    n_paths: int = Field(default=100_000, ge=1_000, le=10_000_000)
    antithetic: bool = True
    # Bump-and-revalue delta and vega for products without pathwise Greeks.
    # Costs 4 extra pricings; the counter-based RNG reuses the same random
    # numbers for each bump, so the differences are smooth, not noise.
    greeks: bool = False


class PriceRequest(Strict):
    instrument_id: str = Field(min_length=1, max_length=64)
    market_data_version: str = Field(min_length=1, max_length=64)
    product: Product
    simulation: Simulation = Simulation()

    @model_validator(mode="after")
    def bounded_work(self):
        if self.simulation.n_paths * self.product.steps > MAX_REQUEST_WORK:
            raise ValueError(f"n_paths x steps exceeds {MAX_REQUEST_WORK:,}; reduce paths or fixings")
        return self


class PriceResponse(BaseModel):
    price: float
    std_err: float
    delta: float | None = None
    vega: float | None = None
    gamma: float | None = None
    reference_price: float | None = None  # closed form, where one exists
    variance_reduction: float | None = None
    n_paths: int
    method: str
    cached: bool
    market_data_version: str
    request_hash: str


class Trade(Strict):
    trade_id: str = Field(min_length=1, max_length=64)
    underlying: str = Field(min_length=1, max_length=32)
    quantity: float = Field(ge=-1e7, le=1e7)
    product: Product


class PortfolioRequest(Strict):
    book_id: str = Field(min_length=1, max_length=64)
    market_data_version: str = Field(min_length=1, max_length=64)
    trades: list[Trade] = Field(min_length=1, max_length=100_000)
    simulation: Simulation = Simulation()
    # Trades per queue message. Too small and Redis round trips dominate; too
    # large and one slow task holds up the whole book (see the benchmark).
    task_size: int = Field(default=100, ge=1, le=5_000)
    include_trades: bool = False
    persist: bool = True
    timeout_s: float = Field(default=60, gt=0, le=600)

    @model_validator(mode="after")
    def bounded_work(self):
        work = sum(t.product.steps for t in self.trades) * self.simulation.n_paths
        if work > MAX_PORTFOLIO_WORK:
            raise ValueError(f"book work {work:,} exceeds {MAX_PORTFOLIO_WORK:,} path-steps")
        return self


class TradeResult(BaseModel):
    trade_id: str
    underlying: str
    quantity: float
    price: float | None = None
    std_err: float | None = None
    delta: float | None = None
    vega: float | None = None
    cached: bool | None = None
    error: str | None = None


class PortfolioResponse(BaseModel):
    job_id: str
    book_id: str
    mode: str
    n_trades: int
    n_failed: int
    n_cached: int
    pv: float
    pv_std_err: float
    delta_by_underlying: dict[str, float]
    vega_by_underlying: dict[str, float]
    tasks: int
    workers_used: dict[str, int]
    elapsed_ms: float
    trades: list[TradeResult] | None = None


class UnderlyingSpec(Strict):
    name: str = Field(min_length=1, max_length=32)
    spot: float = Field(gt=0)
    vol: float = Field(gt=0, le=5.0)
    div_yield: float = Field(default=0.0, ge=0, le=1.0)


class ExposureTrade(Strict):
    trade_id: str = Field(min_length=1, max_length=64)
    underlying: str = Field(min_length=1, max_length=32)
    kind: Literal["call", "put", "forward"]
    strike: float = Field(gt=0)
    maturity: float = Field(gt=0, le=30.0)
    quantity: float = Field(ge=-1e7, le=1e7)


class ExposureRequest(Strict):
    """A netting set's exposure profile (EE, PFE) and a unilateral CVA.

    Simplifications, stated up front: no collateral or margin period of risk,
    flat rate and hazard rate, Black-Scholes revaluation, and no wrong-way risk.
    """

    netting_set_id: str = Field(min_length=1, max_length=64)
    market_data_version: str = Field(min_length=1, max_length=64)
    rate: float = Field(ge=-0.05, le=1.0)
    underlyings: list[UnderlyingSpec] = Field(min_length=1, max_length=50)
    correlation: list[list[float]] | None = None  # identity when omitted
    trades: list[ExposureTrade] = Field(min_length=1, max_length=50_000)
    horizon_years: float = Field(default=5.0, gt=0, le=30.0)
    step_months: int = Field(default=1, ge=1, le=12)
    n_scenarios: int = Field(default=20_000, ge=1_000, le=2_000_000)
    pfe_quantile: float = Field(default=0.95, ge=0.5, lt=1.0)
    recovery: float = Field(default=0.4, ge=0, lt=1.0)
    cds_spread_bps: float = Field(default=100.0, ge=0, le=10_000)
    # Scenarios per task. Also bounds how long a worker is busy with batch
    # work before it can pick up the next intraday task.
    block_size: int = Field(default=5_000, ge=500, le=200_000)

    @property
    def grid(self) -> list[float]:
        n = math.ceil(self.horizon_years * 12 / self.step_months - 1e-9)
        return [min((i + 1) * self.step_months / 12, self.horizon_years) for i in range(n)]

    @property
    def tail_k(self) -> int:
        # PFE at quantile q is the k-th largest of n, k = n - ceil(q n) + 1.
        return self.n_scenarios - math.ceil(self.pfe_quantile * self.n_scenarios) + 1

    @model_validator(mode="after")
    def consistent(self):
        names = [u.name for u in self.underlyings]
        if len(set(names)) != len(names):
            raise ValueError("underlying names must be unique")
        unknown = {t.underlying for t in self.trades} - set(names)
        if unknown:
            raise ValueError(f"trades reference unknown underlyings: {sorted(unknown)}")
        if self.correlation is not None:
            c = np.asarray(self.correlation, dtype=float)
            n = len(names)
            if c.shape != (n, n):
                raise ValueError(f"correlation must be {n}x{n}")
            if not np.allclose(c, c.T, atol=1e-12) or not np.allclose(np.diag(c), 1.0, atol=1e-12):
                raise ValueError("correlation must be symmetric with a unit diagonal")
            if np.any(np.abs(c) > 1):
                raise ValueError("correlations must be in [-1, 1]")
            try:
                np.linalg.cholesky(c)
            except np.linalg.LinAlgError:
                raise ValueError("correlation must be positive definite") from None
        work = self.n_scenarios * len(self.grid) * len(self.trades)
        if work > MAX_EXPOSURE_WORK:
            raise ValueError(f"scenarios x steps x trades = {work:,} exceeds {MAX_EXPOSURE_WORK:,}")
        if len(self.grid) * min(self.tail_k, self.block_size) * 8 > MAX_TAIL_BYTES:
            raise ValueError("PFE tail per block is too large: raise pfe_quantile or reduce block_size")
        return self


class ExposureJob(BaseModel):
    job_id: str
    n_blocks: int
    status_url: str


class ExposureResult(BaseModel):
    job_id: str
    status: Literal["running", "complete", "failed"]
    done: int
    n_blocks: int
    netting_set_id: str | None = None
    grid: list[float] | None = None
    ee: list[float] | None = None
    pfe: list[float] | None = None
    epe: float | None = None
    peak_pfe: float | None = None
    cva: float | None = None
    n_scenarios: int | None = None
    workers_used: dict[str, int] | None = None
    elapsed_ms: float | None = None
    errors: list[str] | None = None

