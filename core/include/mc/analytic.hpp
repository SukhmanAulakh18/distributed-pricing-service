#pragma once

#include <cmath>

#include "mc/random.hpp"

namespace mc {

struct BsResult {
    double price;
    double delta;
    double gamma;
    double vega;
};

// Black-Scholes-Merton with a continuous dividend yield, price and Greeks.
// Throws std::invalid_argument on non-positive spot, strike, vol or maturity.
BsResult black_scholes(double spot, double strike, double rate, double div_yield, double vol,
                       double maturity, bool is_call);

// Unchecked price for hot loops (exposure revaluation, batch pricing). The
// caller guarantees positive inputs.
inline double bs_price_unchecked(double spot, double strike, double rate, double div_yield,
                                 double vol, double tau, bool is_call) {
    const double sd = vol * std::sqrt(tau);
    const double d1 = (std::log(spot / strike) + (rate - div_yield) * tau) / sd + 0.5 * sd;
    const double d2 = d1 - sd;
    const double fwd = spot * std::exp(-div_yield * tau);
    const double pv_strike = strike * std::exp(-rate * tau);
    return is_call ? fwd * norm_cdf(d1) - pv_strike * norm_cdf(d2)
                   : pv_strike * norm_cdf(-d2) - fwd * norm_cdf(-d1);
}

// Geometric-average Asian option with n equally spaced fixings t_i = i T / n.
// The geometric mean of lognormals is lognormal, so this has a closed form;
// it is the control variate for the arithmetic Asian.
double geometric_asian(double spot, double strike, double rate, double div_yield, double vol,
                       double maturity, int n_fixings, bool is_call);

}  // namespace mc
