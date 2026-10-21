#include <gtest/gtest.h>

#include <cmath>
#include <stdexcept>

#include "mc/analytic.hpp"
#include "mc/pricing.hpp"
#include "test_util.hpp"

namespace {

struct Case {
    double s, k, r, q, v, t;
};

const Case kCases[] = {
    {100, 105, 0.03, 0.00, 0.20, 1.0},
    {100, 100, 0.00, 0.02, 0.40, 0.5},
    {50, 40, 0.05, 0.00, 0.10, 2.0},
    {100, 150, 0.02, 0.01, 0.60, 5.0},
};

mc::SimConfig config(std::int64_t n, bool antithetic = true, int threads = 4) {
    return {paths(n), antithetic, true, 11, threads};
}

}  // namespace

TEST(EuropeanMc, PriceAndPathwiseGreeksMatchBlackScholes) {
    for (const auto& c : kCases) {
        for (bool call : {true, false}) {
            for (bool anti : {true, false}) {
                const auto mc_r = mc::price_european_mc(c.s, c.k, c.r, c.q, c.v, c.t, call, config(400'000, anti));
                const auto bs = mc::black_scholes(c.s, c.k, c.r, c.q, c.v, c.t, call);
                EXPECT_NEAR(mc_r.price.value, bs.price, 4 * mc_r.price.std_err);
                EXPECT_NEAR(mc_r.delta.value, bs.delta, 4 * mc_r.delta.std_err);
                EXPECT_NEAR(mc_r.vega.value, bs.vega, 4 * mc_r.vega.std_err);
            }
        }
    }
}

TEST(EuropeanMc, BitIdenticalForAnyThreadCount) {
    const auto base = mc::price_european_mc(100, 105, 0.03, 0, 0.2, 1, true, config(300'001, true, 1));
    for (int threads : {2, 3, 8, 16}) {
        const auto r = mc::price_european_mc(100, 105, 0.03, 0, 0.2, 1, true, config(300'001, true, threads));
        EXPECT_EQ(r.price.value, base.price.value);
        EXPECT_EQ(r.price.std_err, base.price.std_err);
        EXPECT_EQ(r.delta.value, base.delta.value);
    }
}

TEST(EuropeanMc, AntitheticReducesStandardError) {
    const auto plain = mc::price_european_mc(100, 100, 0.03, 0, 0.2, 1, true, config(1'000'000, false));
    const auto anti = mc::price_european_mc(100, 100, 0.03, 0, 0.2, 1, true, config(1'000'000, true));
    EXPECT_LT(anti.price.std_err, plain.price.std_err);
}

TEST(EuropeanMc, StandardErrorShrinksAsOneOverRootN) {
    const auto small = mc::price_european_mc(100, 105, 0.03, 0, 0.2, 1, true, config(100'000, false));
    const auto large = mc::price_european_mc(100, 105, 0.03, 0, 0.2, 1, true, config(1'600'000, false));
    EXPECT_NEAR(small.price.std_err / large.price.std_err, 4.0, 0.2);
}

TEST(AsianMc, ControlVariateAgreesWithPlainAndCutsVariance) {
    for (bool call : {true, false}) {
        mc::SimConfig plain_cfg = config(400'000), cv_cfg = config(400'000);
        plain_cfg.control_variate = false;
        const auto plain = mc::price_asian_mc(100, 100, 0.05, 0, 0.3, 1, 12, call, plain_cfg);
        const auto cv = mc::price_asian_mc(100, 100, 0.05, 0, 0.3, 1, 12, call, cv_cfg);
        const double combined_se = std::hypot(plain.price.std_err, cv.price.std_err);
        EXPECT_NEAR(cv.price.value, plain.price.value, 4 * combined_se);
        EXPECT_GT(cv.variance_reduction, 20.0);
        EXPECT_LT(cv.price.std_err, plain.price.std_err / 4);
    }
}

TEST(AsianMc, OneFixingWithControlVariateIsExactlyBlackScholes) {
    // Arithmetic and geometric averages coincide, so the control removes all
    // the noise and leaves the closed-form price.
    const auto r = mc::price_asian_mc(100, 95, 0.04, 0.01, 0.3, 2.0, 1, true, config(20'000));
    EXPECT_NEAR(r.price.value, mc::black_scholes(100, 95, 0.04, 0.01, 0.3, 2.0, true).price, 1e-9);
}

TEST(AsianMc, AverageIsCheaperThanEuropean) {
    const auto asian = mc::price_asian_mc(100, 100, 0.03, 0, 0.25, 1, 52, true, config(200'000));
    EXPECT_LT(asian.price.value, mc::black_scholes(100, 100, 0.03, 0, 0.25, 1, true).price);
}

TEST(Pricing, RejectsInvalidInputs) {
    EXPECT_THROW(mc::price_european_mc(100, 100, 0.01, 0, -0.2, 1, true, config(1000)), std::invalid_argument);
    EXPECT_THROW(mc::price_european_mc(100, 100, 0.01, 0, 0.2, 1, true, {1, true, true, 1, 1}), std::invalid_argument);
    // Two antithetic paths are one sample: no standard error is possible.
    EXPECT_THROW(mc::price_european_mc(100, 100, 0.01, 0, 0.2, 1, true, {2, true, true, 1, 1}), std::invalid_argument);
    EXPECT_NO_THROW(mc::price_european_mc(100, 100, 0.01, 0, 0.2, 1, true, {4, true, true, 1, 1}));
    EXPECT_THROW(mc::price_european_mc(100, 100, 0.01, 0, 0.2, 1, true, {1000, true, true, 1, 0}), std::invalid_argument);
    EXPECT_THROW(mc::price_asian_mc(100, 100, 0.01, 0, 0.2, 1, 0, true, config(1000)), std::invalid_argument);
}
