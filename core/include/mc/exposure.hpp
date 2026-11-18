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

// A two-way CSA (credit support annex). Collateral called at a node is
// target(V) = max(V - threshold_recv, 0) - max(-V - threshold_post, 0); a
// call is only made when it moves the balance by at least `mta`. The
// collateral held at t is the balance as of t - MPoR (the margin period of
// risk: the counterparty stops posting, we close out later), given per step
// by `lag_index`. The independent amount is posted to us, segregated, so it
// lowers our exposure but does not add to theirs.
struct CsaTerms {
    double threshold_recv = 0.0;
    double threshold_post = 0.0;
    double mta = 0.0;
    double independent_amount = 0.0;
    // lag_index[j]: the step whose balance is held at step j (<= j); -1 is t = 0.
    std::vector<int> lag_index;
};

double csa_target(double v, const CsaTerms& csa);

// Everything needed to merge scenario blocks exactly. Means merge by adding
// sums. Quantiles do not merge, so each block also keeps every time step's
// largest `tail_k` exposures. The global top k is always inside the union of
// the blocks' top k, so the merged PFE is exact. The `csa_` series are empty
// unless a CSA was given; they come from the same scenarios as the rest.
struct ExposureBlock {
    std::vector<double> sum;      // per step: sum of max(V, 0)
    std::vector<double> sum_sq;   // per step: sum of max(V, 0)^2
    std::vector<double> neg_sum;  // per step: sum of max(-V, 0), for DVA
    std::vector<double> tails;    // [step][tail_k], descending
    std::vector<double> csa_sum, csa_sum_sq, csa_neg_sum, csa_tails;
    double v0 = 0.0;  // netting-set value today, the same in every block
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

// The same, plus collateralized exposure under `csa` (nullptr: none).
ExposureBlock simulate_exposure(const std::vector<Underlying>& underlyings,
                                const std::vector<double>& correlation,
                                const std::vector<ExposureTrade>& trades,
                                const std::vector<double>& grid, double rate, std::uint64_t seed,
                                std::int64_t start, std::int64_t count, int tail_k, int threads,
                                const CsaTerms* csa);

}  // namespace mc
