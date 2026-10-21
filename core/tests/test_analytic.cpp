#include <gtest/gtest.h>

#include <cmath>
#include <stdexcept>

#include "mc/analytic.hpp"
#include "mc/random.hpp"
#include "test_util.hpp"

TEST(BlackScholes, TextbookValue) {
    // Hull: S = K = 100, r = 5%, vol = 20%, T = 1 year.
    EXPECT_NEAR(mc::black_scholes(100, 100, 0.05, 0.0, 0.2, 1.0, true).price, 10.4506, 1e-4);
}

TEST(BlackScholes, PutCallParityWithDividends) {
    const double s = 95, k = 105, r = 0.03, q = 0.02, t = 1.7;
    const double c = mc::black_scholes(s, k, r, q, 0.3, t, true).price;
    const double p = mc::black_scholes(s, k, r, q, 0.3, t, false).price;
    EXPECT_NEAR(c - p, s * std::exp(-q * t) - k * std::exp(-r * t), 1e-12);
}

TEST(BlackScholes, GreeksMatchFiniteDifferences) {
    const double s = 100, k = 110, r = 0.02, q = 0.01, v = 0.25, t = 0.75, h = 1e-3;
    for (bool call : {true, false}) {
        auto price = [&](double spot, double vol) { return mc::black_scholes(spot, k, r, q, vol, t, call).price; };
        const auto g = mc::black_scholes(s, k, r, q, v, t, call);
        EXPECT_NEAR(g.delta, (price(s + h, v) - price(s - h, v)) / (2 * h), 1e-7);
        EXPECT_NEAR(g.gamma, (price(s + h, v) - 2 * price(s, v) + price(s - h, v)) / (h * h), 1e-5);
        // Central differences carry O(h^2) truncation error, so bump vol less.
        const double hv = 1e-5;
        EXPECT_NEAR(g.vega, (price(s, v + hv) - price(s, v - hv)) / (2 * hv), 1e-5);
    }
}

TEST(BlackScholes, RejectsInvalidInputs) {
    EXPECT_THROW(mc::black_scholes(-1, 100, 0.01, 0, 0.2, 1, true), std::invalid_argument);
    EXPECT_THROW(mc::black_scholes(100, 100, 0.01, 0, 0.0, 1, true), std::invalid_argument);
    EXPECT_THROW(mc::black_scholes(100, 100, 0.01, 0, 0.2, NAN, true), std::invalid_argument);
}

TEST(GeometricAsian, OneFixingIsEuropean) {
    for (bool call : {true, false}) {
        EXPECT_NEAR(mc::geometric_asian(100, 95, 0.04, 0.01, 0.3, 2.0, 1, call),
                    mc::black_scholes(100, 95, 0.04, 0.01, 0.3, 2.0, call).price, 1e-12);
    }
}

// Independent check of the closed form: brute-force Monte Carlo of the
// geometric average written out directly in the test.
TEST(GeometricAsian, MatchesBruteForceSimulation) {
    const double s = 100, k = 100, r = 0.05, q = 0.0, v = 0.3, t = 1.0;
    const int n_fix = 12;
    const std::int64_t n = paths(400'000);
    const double dt = t / n_fix;
    double sum = 0, sum_sq = 0, u[n_fix];
    for (std::int64_t p = 0; p < n; ++p) {
        mc::path_uniforms(99, p, 0, u, n_fix);
        double log_s = std::log(s), sum_log = 0;
        for (int i = 0; i < n_fix; ++i) {
            log_s += (r - q - 0.5 * v * v) * dt + v * std::sqrt(dt) * mc::inv_norm_cdf(u[i]);
            sum_log += log_s;
        }
        const double payoff = std::max(std::exp(sum_log / n_fix) - k, 0.0) * std::exp(-r * t);
        sum += payoff;
        sum_sq += payoff * payoff;
    }
    const double mean = sum / n, se = std::sqrt((sum_sq / n - mean * mean) / n);
    EXPECT_NEAR(mc::geometric_asian(s, k, r, q, v, t, n_fix, true), mean, 4 * se);
}
