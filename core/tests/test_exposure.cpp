#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include "mc/analytic.hpp"
#include "mc/exposure.hpp"
#include "mc/random.hpp"
#include "test_util.hpp"

using mc::ExposureTrade;
using mc::TradeKind;
using mc::Underlying;

namespace {

const std::vector<Underlying> kOne = {{100.0, 0.25, 0.01}};
const std::vector<double> kIdentity1 = {1.0};

std::vector<double> monthly(int months) {
    std::vector<double> g;
    for (int m = 1; m <= months; ++m) g.push_back(m / 12.0);
    return g;
}

}  // namespace

TEST(Cholesky, ReconstructsTheMatrix) {
    const std::vector<double> a = {1.0, 0.6, 0.3, 0.6, 1.0, -0.2, 0.3, -0.2, 1.0};
    const auto l = mc::cholesky(a, 3);
    for (int i = 0; i < 3; ++i) {
        for (int j = 0; j < 3; ++j) {
            double s = 0;
            for (int k = 0; k < 3; ++k) s += l[i * 3 + k] * l[j * 3 + k];
            EXPECT_NEAR(s, a[i * 3 + j], 1e-14);
        }
    }
}

TEST(Cholesky, RejectsInvalidCorrelation) {
    EXPECT_THROW(mc::cholesky({1.0, 0.9, 0.9, 0.9, 1.0, -0.9, 0.9, -0.9, 1.0}, 3), std::invalid_argument);
    EXPECT_THROW(mc::cholesky({1.0, 0.5, 0.4, 1.0}, 2), std::invalid_argument);  // not symmetric
    EXPECT_THROW(mc::cholesky({2.0, 0.0, 0.0, 1.0}, 2), std::invalid_argument);  // diagonal != 1
}

// A long call is worth more than zero in every scenario, so its exposure is
// its value, and the discounted value is a martingale: EE(t) = C(0) e^{rt}.
TEST(Exposure, LongCallExpectedExposureIsForwardValue) {
    const double r = 0.03;
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Call, 105.0, 2.0, 10.0}};
    const auto grid = monthly(18);
    const std::int64_t n = paths(100'000);
    const auto b = mc::simulate_exposure(kOne, kIdentity1, trades, grid, r, 5, 0, n, 0, 4);
    const double c0 = 10.0 * mc::black_scholes(100, 105, r, 0.01, 0.25, 2.0, true).price;
    for (std::size_t j = 0; j < grid.size(); ++j) {
        const double ee = b.sum[j] / n;
        const double se = std::sqrt((b.sum_sq[j] / n - ee * ee) / n);
        EXPECT_NEAR(ee, c0 * std::exp(r * grid[j]), 4 * se) << "t=" << grid[j];
    }
}

// A forward's value at t is linear in S(t), so E[max(V, 0)] is a Black
// formula in S(t): an independent closed form for the expected exposure.
TEST(Exposure, ForwardExpectedExposureMatchesClosedForm) {
    const double r = 0.02, q = 0.01, vol = 0.25, k = 100.0, mat = 1.0, qty = 3.0;
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Forward, k, mat, qty}};
    const std::vector<double> grid = {0.25, 0.5, 0.75};
    const std::int64_t n = paths(200'000);
    const auto b = mc::simulate_exposure(kOne, kIdentity1, trades, grid, r, 9, 0, n, 0, 2);
    for (std::size_t j = 0; j < grid.size(); ++j) {
        const double t = grid[j], tau = mat - t;
        const double a = std::exp(-q * tau), c = k * std::exp(-r * tau);
        const double sd = vol * std::sqrt(t);
        const double d1 = (std::log(a * 100.0 / c) + (r - q) * t) / sd + 0.5 * sd;
        const double expected = qty * (a * 100.0 * std::exp((r - q) * t) * mc::norm_cdf(d1) - c * mc::norm_cdf(d1 - sd));
        const double ee = b.sum[j] / n;
        const double se = std::sqrt((b.sum_sq[j] / n - ee * ee) / n);
        EXPECT_NEAR(ee, expected, 4 * se) << "t=" << t;
    }
}

// On its maturity node a trade is still owed its payoff; after it, nothing.
TEST(Exposure, ExpiredTradesDropOut) {
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Call, 100.0, 0.5, 1.0}};
    const auto b = mc::simulate_exposure(kOne, kIdentity1, trades, {0.25, 0.5, 0.75}, 0.01, 1, 0, 2000, 10, 2);
    EXPECT_GT(b.sum[0], 0.0);
    EXPECT_GT(b.sum[1], 0.0);  // intrinsic value on the maturity date
    EXPECT_EQ(b.sum[2], 0.0);
}

// A short call is always worth less than zero, so its negative exposure (the
// counterparty's exposure to us, which drives DVA) is the call's forward value.
TEST(Exposure, ShortCallNegativeExposureIsForwardValue) {
    const double r = 0.03;
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Call, 105.0, 2.0, -10.0}};
    const auto grid = monthly(12);
    const std::int64_t n = paths(100'000);
    const auto b = mc::simulate_exposure(kOne, kIdentity1, trades, grid, r, 5, 0, n, 0, 4);
    const double c0 = 10.0 * mc::black_scholes(100, 105, r, 0.01, 0.25, 2.0, true).price;
    EXPECT_NEAR(b.v0, -c0, 1e-12);
    for (std::size_t j = 0; j < grid.size(); ++j) {
        EXPECT_EQ(b.sum[j], 0.0);
        // Same scenarios as the long-call test, mirrored: the bound comes from its spread.
        EXPECT_NEAR(b.neg_sum[j] / n, c0 * std::exp(r * grid[j]), 0.02 * c0) << "t=" << grid[j];
    }
}

namespace {

mc::CsaTerms csa_terms(double h_recv, double h_post, double mta, double ia, std::vector<int> lag) {
    mc::CsaTerms c;
    c.threshold_recv = h_recv;
    c.threshold_post = h_post;
    c.mta = mta;
    c.independent_amount = ia;
    c.lag_index = std::move(lag);
    return c;
}

std::vector<int> lagged(int steps, int by) {
    std::vector<int> lag(steps);
    for (int j = 0; j < steps; ++j) lag[j] = j - by;  // -1 or less: t = 0
    for (auto& l : lag) l = std::max(l, -1);
    return lag;
}

}  // namespace

// Thresholds no exposure can reach: no collateral ever moves, so the
// collateralized series is the uncollateralized one, bit for bit.
TEST(Csa, InfiniteThresholdChangesNothing) {
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Forward, 100, 1.0, 4}, {0, TradeKind::Put, 90, 2.0, 3}};
    const auto grid = monthly(18);
    const auto csa = csa_terms(1e300, 1e300, 0, 0, lagged(18, 1));
    const auto b = mc::simulate_exposure(kOne, kIdentity1, trades, grid, 0.02, 3, 0, 5000, 50, 3, &csa);
    EXPECT_EQ(b.csa_sum, b.sum);
    EXPECT_EQ(b.csa_sum_sq, b.sum_sq);
    EXPECT_EQ(b.csa_neg_sum, b.neg_sum);
    EXPECT_EQ(b.csa_tails, b.tails);
}

// Zero thresholds and MTA with no margin period: collateral always equals
// the value, so nothing is left at risk.
TEST(Csa, PerfectCollateralLeavesNoExposure) {
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Call, 100, 1.5, 7}, {0, TradeKind::Forward, 95, 1.0, -3}};
    const auto grid = monthly(12);
    const auto csa = csa_terms(0, 0, 0, 0, lagged(12, 0));
    const auto b = mc::simulate_exposure(kOne, kIdentity1, trades, grid, 0.02, 3, 0, 4000, 20, 2, &csa);
    for (std::size_t j = 0; j < grid.size(); ++j) {
        EXPECT_GT(b.sum[j], 0.0);
        EXPECT_EQ(b.csa_sum[j], 0.0);
        EXPECT_EQ(b.csa_neg_sum[j], 0.0);
    }
}

// Independent reference: the CSA rules written out scenario by scenario in
// the simplest way, on the same random numbers, for one underlying with a
// call and a forward, a threshold each way, an MTA, an independent amount
// and a two-step margin period.
TEST(Csa, MatchesBruteForceReference) {
    const double r = 0.02, s0 = 100, vol = 0.25, q = 0.01;
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Call, 100, 1.0, 10}, {0, TradeKind::Forward, 102, 1.5, -6}};
    std::vector<double> grid;
    for (int i = 1; i <= 36; ++i) grid.push_back(i / 24.0);  // half-monthly, past both maturities
    const int steps = static_cast<int>(grid.size());
    const auto csa = csa_terms(15, 25, 4, 2, lagged(steps, 2));
    const std::int64_t n = 3000;
    const auto b = mc::simulate_exposure(kOne, kIdentity1, trades, grid, r, 11, 0, n, 0, 3, &csa);

    auto value = [&](double s, double t) {
        double v = 0;
        for (const auto& tr : trades) {
            const double tau = tr.maturity - t;
            if (tau < -1e-12) continue;
            if (tau <= 1e-12) {
                v += tr.quantity * (tr.kind == TradeKind::Call ? std::max(s - tr.strike, 0.0) : s - tr.strike);
            } else if (tr.kind == TradeKind::Forward) {
                v += tr.quantity * (s * std::exp(-q * tau) - tr.strike * std::exp(-r * tau));
            } else {
                v += tr.quantity * mc::black_scholes(s, tr.strike, r, q, vol, tau, true).price;
            }
        }
        return v;
    };
    auto target = [](double v) { return std::max(v - 15.0, 0.0) - std::max(-v - 25.0, 0.0); };

    std::vector<double> ref_sum(steps, 0.0), ref_neg(steps, 0.0);
    for (std::int64_t i = 0; i < n; ++i) {
        std::vector<double> bal(steps);
        const double bal0 = target(value(s0, 0.0));
        double log_s = std::log(s0), t_prev = 0;
        for (int j = 0; j < steps; ++j) {
            double u;
            mc::path_uniforms(11, i, static_cast<std::uint32_t>(j + 1), &u, 1);
            const double dt = grid[j] - t_prev;
            log_s += (r - q - 0.5 * vol * vol) * dt + vol * std::sqrt(dt) * mc::inv_norm_cdf(u);
            t_prev = grid[j];
            const double v = value(std::exp(log_s), grid[j]);
            const double prev = j ? bal[j - 1] : bal0;
            bal[j] = std::abs(target(v) - prev) >= 4 ? target(v) : prev;
            const double held = j >= 2 ? bal[j - 2] : bal0;
            ref_sum[j] += std::max(v - held - 2.0, 0.0);
            ref_neg[j] += std::max(held - v, 0.0);
        }
    }
    for (int j = 0; j < steps; ++j) {
        EXPECT_NEAR(b.csa_sum[j], ref_sum[j], 1e-9 * std::max(1.0, ref_sum[j])) << "step " << j;
        EXPECT_NEAR(b.csa_neg_sum[j], ref_neg[j], 1e-9 * std::max(1.0, ref_neg[j])) << "step " << j;
    }
    // A margin period of two half-months leaves real exposure; the CSA still
    // removes most of it.
    double ee = 0, ee_csa = 0;
    for (int j = 0; j < steps; ++j) ee += b.sum[j], ee_csa += b.csa_sum[j];
    EXPECT_GT(ee_csa, 0.0);
    EXPECT_LT(ee_csa, 0.6 * ee);
}

// Collateralized PFE tails merge across blocks exactly, like the rest.
TEST(Csa, BlocksMergeToTheSingleRunExactly) {
    const std::vector<Underlying> und = {{100, 0.2, 0.0}, {50, 0.35, 0.02}};
    const std::vector<double> corr = {1.0, 0.5, 0.5, 1.0};
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Call, 100, 1.0, 5}, {1, TradeKind::Put, 55, 2.0, -8}};
    const auto grid = monthly(24);
    const auto csa = csa_terms(5, 5, 1, 0, lagged(24, 1));
    const std::int64_t n = 4096;
    const int k = 205;
    const auto whole = mc::simulate_exposure(und, corr, trades, grid, 0.02, 7, 0, n, k, 3, &csa);
    const auto a = mc::simulate_exposure(und, corr, trades, grid, 0.02, 7, 0, 1500, k, 1, &csa);
    const auto b = mc::simulate_exposure(und, corr, trades, grid, 0.02, 7, 1500, n - 1500, k, 4, &csa);
    for (std::size_t j = 0; j < grid.size(); ++j) {
        EXPECT_NEAR(a.csa_sum[j] + b.csa_sum[j], whole.csa_sum[j], 1e-9 * std::max(1.0, whole.csa_sum[j]));
        std::vector<double> merged(a.csa_tails.begin() + j * k, a.csa_tails.begin() + (j + 1) * k);
        merged.insert(merged.end(), b.csa_tails.begin() + j * k, b.csa_tails.begin() + (j + 1) * k);
        std::sort(merged.begin(), merged.end(), std::greater<>());
        for (int i = 0; i < k; ++i) EXPECT_EQ(merged[i], whole.csa_tails[j * k + i]);
    }
}

TEST(Csa, RejectsInvalidTerms) {
    const std::vector<ExposureTrade> ok = {{0, TradeKind::Call, 100, 1, 1}};
    const auto neg = csa_terms(-1, 0, 0, 0, {0});
    EXPECT_THROW(mc::simulate_exposure(kOne, kIdentity1, ok, {0.5}, 0.01, 1, 0, 10, 1, 1, &neg), std::invalid_argument);
    const auto future = csa_terms(0, 0, 0, 0, {1});  // a lag cannot look ahead
    EXPECT_THROW(mc::simulate_exposure(kOne, kIdentity1, ok, {0.5}, 0.01, 1, 0, 10, 1, 1, &future), std::invalid_argument);
    const auto short_lag = csa_terms(0, 0, 0, 0, {});
    EXPECT_THROW(mc::simulate_exposure(kOne, kIdentity1, ok, {0.5}, 0.01, 1, 0, 10, 1, 1, &short_lag), std::invalid_argument);
}

// The distributed-merge guarantee: splitting scenarios into blocks (as the
// workers do) reproduces the single run. Tails, and so PFE, are bit-identical.
TEST(Exposure, BlocksMergeToTheSingleRunExactly) {
    const std::vector<Underlying> und = {{100, 0.2, 0.0}, {50, 0.35, 0.02}};
    const std::vector<double> corr = {1.0, 0.5, 0.5, 1.0};
    const std::vector<ExposureTrade> trades = {
        {0, TradeKind::Call, 100, 1.0, 5}, {1, TradeKind::Put, 55, 2.0, -8}, {0, TradeKind::Forward, 98, 1.5, -2}};
    const auto grid = monthly(24);
    const std::int64_t n = 8192;
    const int k = 410;  // 95% PFE needs the top 5% (+1)
    const auto whole = mc::simulate_exposure(und, corr, trades, grid, 0.02, 77, 0, n, k, 3);
    const auto a = mc::simulate_exposure(und, corr, trades, grid, 0.02, 77, 0, 3072, k, 1);
    const auto b = mc::simulate_exposure(und, corr, trades, grid, 0.02, 77, 3072, n - 3072, k, 4);
    for (std::size_t j = 0; j < grid.size(); ++j) {
        EXPECT_NEAR(a.sum[j] + b.sum[j], whole.sum[j], 1e-9 * std::max(1.0, whole.sum[j]));
        std::vector<double> merged(a.tails.begin() + j * k, a.tails.begin() + (j + 1) * k);
        merged.insert(merged.end(), b.tails.begin() + j * k, b.tails.begin() + (j + 1) * k);
        std::sort(merged.begin(), merged.end(), std::greater<>());
        for (int i = 0; i < k; ++i) EXPECT_EQ(merged[i], whole.tails[j * k + i]);
    }
}

TEST(Exposure, RejectsInvalidInputs) {
    const std::vector<ExposureTrade> bad_ref = {{3, TradeKind::Call, 100, 1, 1}};
    EXPECT_THROW(mc::simulate_exposure(kOne, kIdentity1, bad_ref, {0.5}, 0.01, 1, 0, 10, 1, 1), std::invalid_argument);
    const std::vector<ExposureTrade> ok = {{0, TradeKind::Call, 100, 1, 1}};
    EXPECT_THROW(mc::simulate_exposure(kOne, kIdentity1, ok, {0.5, 0.5}, 0.01, 1, 0, 10, 1, 1), std::invalid_argument);
    EXPECT_THROW(mc::simulate_exposure(kOne, {1.0, 0.0}, ok, {0.5}, 0.01, 1, 0, 10, 1, 1), std::invalid_argument);
}
