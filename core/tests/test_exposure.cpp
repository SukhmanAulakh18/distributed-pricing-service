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

TEST(Exposure, ExpiredTradesDropOut) {
    const std::vector<ExposureTrade> trades = {{0, TradeKind::Call, 100.0, 0.5, 1.0}};
    const auto b = mc::simulate_exposure(kOne, kIdentity1, trades, {0.25, 0.5, 0.75}, 0.01, 1, 0, 2000, 10, 2);
    EXPECT_GT(b.sum[0], 0.0);
    EXPECT_EQ(b.sum[1], 0.0);
    EXPECT_EQ(b.sum[2], 0.0);
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
