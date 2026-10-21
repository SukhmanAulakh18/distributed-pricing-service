#pragma once

#include <cstdint>

#include "mc/stats.hpp"

namespace mc {

struct SimConfig {
    std::int64_t n_paths = 100'000;
    bool antithetic = true;
    bool control_variate = true;  // Asian only
    std::uint64_t seed = 42;
    int threads = 1;
};

struct McResult {
    Estimate price;
    Estimate delta;                   // European only: pathwise estimator
    Estimate vega;                    // European only: pathwise estimator
    double variance_reduction = 1.0;  // Var(plain) / Var(estimator used); control variate only
    std::int64_t n_paths = 0;
};

// European option under GBM, terminal-value simulation.
McResult price_european_mc(double spot, double strike, double rate, double div_yield, double vol,
                           double maturity, bool is_call, const SimConfig& cfg);

// Arithmetic-average Asian option with n equally spaced fixings. With
// cfg.control_variate, the closed-form geometric Asian is used as a control.
McResult price_asian_mc(double spot, double strike, double rate, double div_yield, double vol,
                        double maturity, int n_fixings, bool is_call, const SimConfig& cfg);

}  // namespace mc
