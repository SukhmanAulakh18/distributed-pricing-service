import math
from fractions import Fraction
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
# All blocks' PFE tails together pass through Redis and the API-side merge.
MAX_TOTAL_TAIL_BYTES = 1024 * 1024 * 1024


class Strict(BaseModel):
    # JSON allows Infinity and NaN; "gt=0" lets infinity through, and it
    # comes back out as a NaN price.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


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


class CsaSpec(Strict):
    """A two-way CSA: the counterparty posts once our exposure passes
    threshold_recv, we post once theirs passes threshold_post, calls smaller
    than the MTA are not made, and on default collateral stops moving for the
    margin period of risk before close-out."""

    threshold_recv: float = Field(default=0.0, ge=0, le=1e12)
    threshold_post: float = Field(default=0.0, ge=0, le=1e12)
    mta: float = Field(default=0.0, ge=0, le=1e12)
    independent_amount: float = Field(default=0.0, ge=0, le=1e12)
    mpor_days: int = Field(default=10, ge=0, le=60)  # 10 business days: the Basel floor for most netting sets


class OwnCredit(Strict):
    """Our own credit, for DVA and a bilateral CVA."""

    recovery: float = Field(default=0.4, ge=0, lt=1.0)
    cds_spread_bps: float = Field(default=50.0, ge=0, le=10_000)


class ExposureRequest(Strict):
    """A netting set's exposure profile (EE, PFE), CVA, and optionally the
    same under a CSA and a DVA / bilateral CVA from our own credit.

    Simplifications, stated up front: flat rate and hazard rates, independent
    defaults, Black-Scholes revaluation, risk-neutral PFE, and no wrong-way risk.
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
    csa: CsaSpec | None = None
    own_credit: OwnCredit | None = None
    # Scenarios per task. Also bounds how long a worker is busy with batch
    # work before it can pick up the next intraday task.
    block_size: int = Field(default=5_000, ge=500, le=200_000)

    @property
    def grid(self) -> list[float]:
        n = math.ceil(self.horizon_years * 12 / self.step_months - 1e-9)
        return [min((i + 1) * self.step_months / 12, self.horizon_years) for i in range(n)]

    @property
    def sim_grid(self) -> tuple[list[float], list[int], list[int]]:
        """(nodes simulated, lag_index per node, positions of `grid` in it).

        With a CSA, each reporting date t also needs a node at t - MPoR, the
        last date collateral moved. A monthly grid cannot resolve 10 days, so
        those nodes are added. lag_index[j] is the last node at or before
        t_j - MPoR, or -1 for today.
        """
        grid = self.grid
        if self.csa is None:
            return grid, [], list(range(len(grid)))
        delta = self.csa.mpor_days / 365.0
        # Reporting dates stay exact; a lag node within 1e-9 of an existing
        # node is that node (t - 0 must not become a second node an ulp away).
        nodes = list(grid)
        for t in grid:
            if t - delta > 1e-9 and all(abs(u - (t - delta)) > 1e-9 for u in nodes):
                nodes.append(t - delta)
        nodes.sort()
        lag = [max((i for i, u in enumerate(nodes) if u <= t - delta + 1e-9), default=-1) for t in nodes]
        pos = {t: i for i, t in enumerate(nodes)}
        return nodes, lag, [pos[t] for t in grid]

    @property
    def tail_k(self) -> int:
        # PFE at quantile q is the k-th largest of n, k = n - ceil(q n) + 1.
        # Exact rational arithmetic: 0.95 * 20_000 must be 19_000, not 19_000.000000000004.
        return self.n_scenarios - math.ceil(Fraction(repr(self.pfe_quantile)) * self.n_scenarios) + 1

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
            # Same tolerances as the C++ core (exposure.cpp: cholesky), so a
            # matrix accepted here never fails later inside every worker.
            if not np.allclose(c, c.T, rtol=0, atol=1e-12) or not np.allclose(np.diag(c), 1.0, rtol=0, atol=1e-12):
                raise ValueError("correlation must be symmetric with a unit diagonal")
            if np.any(np.abs(c) > 1):
                raise ValueError("correlations must be in [-1, 1]")
            try:
                pivots = np.diag(np.linalg.cholesky(c)) ** 2
            except np.linalg.LinAlgError:
                pivots = np.zeros(1)
            if pivots.min() <= 1e-12:
                raise ValueError("correlation must be positive definite") from None
        steps = len(self.sim_grid[0])
        work = self.n_scenarios * steps * len(self.trades)
        if work > MAX_EXPOSURE_WORK:
            raise ValueError(f"scenarios x steps x trades = {work:,} exceeds {MAX_EXPOSURE_WORK:,}")
        series = 2 if self.csa else 1
        block_tail = steps * min(self.tail_k, self.block_size) * 8 * series
        if block_tail > MAX_TAIL_BYTES:
            raise ValueError("PFE tail per block is too large: raise pfe_quantile or reduce block_size")
        if block_tail * math.ceil(self.n_scenarios / self.block_size) > MAX_TOTAL_TAIL_BYTES:
            raise ValueError("PFE tails across all blocks are too large: raise pfe_quantile")
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
    ee_se: list[float] | None = None  # Monte Carlo standard error of each EE point
    ene: list[float] | None = None  # expected negative exposure: what we owe them
    v0: float | None = None  # netting-set value today
    dva: float | None = None
    bilateral_cva: float | None = None  # CVA - DVA, with first-to-default survival
    # Under the CSA, from the same scenarios:
    ee_csa: list[float] | None = None
    pfe_csa: list[float] | None = None
    epe_csa: float | None = None
    peak_pfe_csa: float | None = None
    cva_csa: float | None = None
    dva_csa: float | None = None
    bilateral_cva_csa: float | None = None
    n_scenarios: int | None = None
    workers_used: dict[str, int] | None = None
    elapsed_ms: float | None = None
    errors: list[str] | None = None



MAX_PREDEAL_SCENARIOS = 50_000  # runs synchronously on an API pod: a quote, not a batch run


class PredealRequest(Strict):
    """Should we do this trade? The netting set as it stands, the new trade,
    and the client's credit limit on peak PFE. `fallback_csa` is the CSA we
    would ask for if the limit is breached without one."""

    netting_set: ExposureRequest
    new_trade: ExposureTrade
    credit_limit: float | None = Field(default=None, gt=0, le=1e13)
    fallback_csa: CsaSpec | None = None

    @model_validator(mode="after")
    def consistent(self):
        ns = self.netting_set
        if ns.n_scenarios > MAX_PREDEAL_SCENARIOS:
            raise ValueError(f"pre-deal checks run synchronously: n_scenarios <= {MAX_PREDEAL_SCENARIOS:,}")
        if self.new_trade.underlying not in {u.name for u in ns.underlyings}:
            raise ValueError(f"new_trade references unknown underlying {self.new_trade.underlying!r}")
        if self.new_trade.trade_id in {t.trade_id for t in ns.trades}:
            raise ValueError("new_trade.trade_id is already in the netting set")
        if self.fallback_csa is not None and ns.csa is not None:
            raise ValueError("fallback_csa only applies to a netting set without a CSA")
        return self


class PredealResult(BaseModel):
    verdict: Literal["approve", "approve_with_csa", "refer_to_credit", "no_limit"]
    reason: str
    incremental_cva: float  # on the limit's basis: collateralized if the netting set has a CSA
    incremental_cva_uncollateralized: float
    incremental_cva_csa: float | None = None
    cva_charge_bps: float  # incremental CVA over the new trade's notional (|quantity| x spot)
    peak_pfe_before: float
    peak_pfe_after: float
    peak_pfe_after_csa: float | None = None  # under the netting set's CSA, or fallback_csa if the limit was breached
    limit_basis: Literal["collateralized", "uncollateralized"]
    limit_utilisation: float | None = None
    risk_reducing: bool
    baseline_cached: bool  # the book's own run came from the cache
    n_scenarios: int
    elapsed_ms: float
