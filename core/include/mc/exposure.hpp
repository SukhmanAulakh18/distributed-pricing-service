#pragma once

#include <cstdint>
#include <vector>

namespace mc {

struct Underlying {
    double spot;
    double vol;
    double div_yield;
};

enum class TradeKind : int { Call = 0, Put = 1, Forward = 2 };

struct ExposureTrade {
    int underlying;  // index into the underlyings
    TradeKind kind;
    double strike;
    double maturity;
    double quantity;
};

// Everything needed to merge scenario blocks exactly. Means merge by adding
// sums. Quantiles do not merge, so each block also keeps every time step's
// largest `tail_k` exposures. The global top k is always inside the union of
// the blocks' top k, so the merged PFE is exact.
struct ExposureBlock {
    std::vector<double> sum;     // per step: sum of max(V, 0)
    std::vector<double> sum_sq;  // per step: sum of max(V, 0)^2
    std::vector<double> tails;   // [step][tail_k], descending
    int tail_k = 0;
    std::int64_t n = 0;
};

// Lower Cholesky factor of a correlation matrix (row-major n x n). Throws if
// it is not symmetric with a unit diagonal and positive definite.
std::vector<double> cholesky(const std::vector<double>& a, int n);

// Simulates scenarios [start, start + count) of a netting set: correlated GBM
// for each underlying on `grid`, full revaluation of every trade at each step
// (Black-Scholes for options, closed form for forwards), exposure max(V, 0).
// Scenario i always uses the same random numbers, so any split into blocks,
// on any number of workers or threads, reproduces the single-run result.
ExposureBlock simulate_exposure(const std::vector<Underlying>& underlyings,
                                const std::vector<double>& correlation,
                                const std::vector<ExposureTrade>& trades,
                                const std::vector<double>& grid, double rate, std::uint64_t seed,
                                std::int64_t start, std::int64_t count, int tail_k, int threads);

}  // namespace mc
