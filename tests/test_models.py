import pytest
from pydantic import ValidationError

from service.models import ExposureRequest, PortfolioRequest, PriceRequest

EURO = {"type": "european", "spot": 100, "strike": 105, "rate": 0.03, "vol": 0.2, "maturity": 1.0}


def price_req(**kw):
    return {"instrument_id": "X", "market_data_version": "md", "product": EURO, **kw}


def test_discriminator_picks_the_product():
    assert PriceRequest(**price_req()).product.type == "european"
    asian = PriceRequest(**price_req(product={**EURO, "type": "asian", "n_fixings": 52}))
    assert asian.product.steps == 52


@pytest.mark.parametrize("product", [{**EURO, "type": "binary"}, {k: v for k, v in EURO.items() if k != "type"}])
def test_unknown_or_missing_type_rejected(product):
    with pytest.raises(ValidationError):
        PriceRequest(**price_req(product=product))


def test_asian_fields_not_accepted_on_european():
    with pytest.raises(ValidationError):
        PriceRequest(**price_req(product={**EURO, "n_fixings": 12}))


def test_work_cap_rejects_runaway_requests():
    product = {**EURO, "type": "asian", "n_fixings": 1000}
    with pytest.raises(ValidationError, match="exceeds"):
        PriceRequest(**price_req(product=product, simulation={"n_paths": 10_000_000}))


def test_analytic_european_needs_no_simulation_budget():
    req = PriceRequest(**price_req(product={**EURO, "method": "analytic"}, simulation={"n_paths": 10_000_000}))
    assert req.product.steps == 0


def test_portfolio_budget():
    trade = {"trade_id": "t", "underlying": "U", "quantity": 1,
             "product": {**EURO, "type": "asian", "n_fixings": 1000}}
    with pytest.raises(ValidationError, match="book work"):
        PortfolioRequest(book_id="b", market_data_version="md", trades=[trade] * 6000,
                         simulation={"n_paths": 10_000_000})


def exposure_req(**kw):
    base = {"netting_set_id": "NS", "market_data_version": "md", "rate": 0.02,
            "underlyings": [{"name": "A", "spot": 100, "vol": 0.2}, {"name": "B", "spot": 50, "vol": 0.3}],
            "trades": [{"trade_id": "t1", "underlying": "A", "kind": "call", "strike": 100, "maturity": 1, "quantity": 1}],
            "horizon_years": 1.0}
    return {**base, **kw}


def test_exposure_grid_and_tail_size():
    req = ExposureRequest(**exposure_req(horizon_years=0.3, n_scenarios=10_000, pfe_quantile=0.95))
    assert req.grid == pytest.approx([1 / 12, 2 / 12, 3 / 12, 0.3])
    assert req.tail_k == 10_000 - 9_500 + 1


@pytest.mark.parametrize("corr, msg", [
    ([[1, 0.5], [0.4, 1]], "symmetric"),
    ([[1, 1.2], [1.2, 1]], r"\[-1, 1\]"),
    ([[1, 0], [0, 1], [0, 0]], "2x2"),
])
def test_exposure_correlation_validated(corr, msg):
    with pytest.raises(ValidationError, match=msg):
        ExposureRequest(**exposure_req(correlation=corr))


def test_exposure_correlation_must_be_positive_definite():
    three = [{"name": n, "spot": 100, "vol": 0.2} for n in "ABC"]
    corr = [[1, 0.9, 0.9], [0.9, 1, -0.9], [0.9, -0.9, 1]]
    with pytest.raises(ValidationError, match="positive definite"):
        ExposureRequest(**exposure_req(underlyings=three, correlation=corr))


def test_exposure_unknown_underlying_rejected():
    bad = [{"trade_id": "t", "underlying": "Z", "kind": "put", "strike": 1, "maturity": 1, "quantity": 1}]
    with pytest.raises(ValidationError, match="unknown underlyings"):
        ExposureRequest(**exposure_req(trades=bad))
