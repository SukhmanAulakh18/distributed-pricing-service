#include "mc/analytic.hpp"

#include <stdexcept>

namespace mc {
namespace {

void check(double spot, double strike, double vol, double maturity) {
    // !(x > 0) also rejects NaN.
    if (!(spot > 0) || !(strike > 0) || !(vol > 0) || !(maturity > 0)) {
        throw std::invalid_argument("spot, strike, vol and maturity must be positive");
    }
}

}  // namespace

BsResult black_scholes(double spot, double strike, double rate, double div_yield, double vol,
                       double maturity, bool is_call) {
    check(spot, strike, vol, maturity);
    const double sqrt_t = std::sqrt(maturity);
    const double d1 = (std::log(spot / strike) + (rate - div_yield + 0.5 * vol * vol) * maturity) /
                      (vol * sqrt_t);
    const double div_df = std::exp(-div_yield * maturity);
    const double gamma = div_df * norm_pdf(d1) / (spot * vol * sqrt_t);
    const double vega = spot * div_df * norm_pdf(d1) * sqrt_t;
    const double price =
        bs_price_unchecked(spot, strike, rate, div_yield, vol, maturity, is_call);
    const double delta = is_call ? div_df * norm_cdf(d1) : -div_df * norm_cdf(-d1);
    return {price, delta, gamma, vega};
}

double geometric_asian(double spot, double strike, double rate, double div_yield, double vol,
                       double maturity, int n_fixings, bool is_call) {
    check(spot, strike, vol, maturity);
    if (n_fixings < 1) throw std::invalid_argument("n_fixings must be at least 1");
    const double n = n_fixings;
    // log G ~ N(mu, s2): the mean of log S(t_i) and the variance of
    // sigma * mean(W(t_i)), using sum_{i,j} min(i, j) = n(n+1)(2n+1)/6.
    const double mu = std::log(spot) +
                      (rate - div_yield - 0.5 * vol * vol) * maturity * (n + 1) / (2 * n);
    const double s2 = vol * vol * maturity * (n + 1) * (2 * n + 1) / (6 * n * n);
    const double s = std::sqrt(s2);
    const double fwd = std::exp(mu + 0.5 * s2);
    const double d1 = (mu - std::log(strike) + s2) / s;
    const double d2 = d1 - s;
    const double df = std::exp(-rate * maturity);
    return is_call ? df * (fwd * norm_cdf(d1) - strike * norm_cdf(d2))
                   : df * (strike * norm_cdf(-d2) - fwd * norm_cdf(-d1));
}

}  // namespace mc
