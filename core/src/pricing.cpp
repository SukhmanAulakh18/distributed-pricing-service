#include "mc/pricing.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <vector>

#include "mc/aligned_buffer.hpp"
#include "mc/analytic.hpp"
#include "mc/parallel.hpp"
#include "mc/random.hpp"

namespace mc {
namespace {

// Samples per task. Fixed, so the floating-point summation order (and hence
// every bit of the result) never depends on the thread count.
constexpr std::int64_t kChunk = 8192;
constexpr int kMaxFixings = 1000;

void check(double spot, double strike, double vol, double maturity, const SimConfig& cfg) {
    if (!(spot > 0) || !(strike > 0) || !(vol > 0) || !(maturity > 0)) {
        throw std::invalid_argument("spot, strike, vol and maturity must be positive");
    }
    // The standard error needs at least two samples (n - 1 in the variance).
    if (cfg.n_paths < (cfg.antithetic ? 4 : 2)) {
        throw std::invalid_argument("n_paths must be at least 2 (4 with antithetic variates)");
    }
    if (cfg.threads < 1) throw std::invalid_argument("threads must be at least 1");
}

std::int64_t samples(const SimConfig& cfg) {
    return cfg.antithetic ? cfg.n_paths / 2 : cfg.n_paths;
}

// Runs path_fn(u, out) over every sample. path_fn gets `dims` uniforms and
// writes `outputs` values. With antithetic variates a sample is the average of
// the paths driven by u and 1 - u (z and -z after the inverse normal).
template <class PathFn>
Moments simulate(const SimConfig& cfg, int dims, int outputs, PathFn path_fn) {
    const std::int64_t n = samples(cfg);
    const std::int64_t n_chunks = (n + kChunk - 1) / kChunk;
    const int threads = effective_threads(n_chunks, cfg.threads);

    // One allocation for all per-thread scratch, each slice cache-line aligned.
    const std::size_t stride = cache_line_stride<double>(2 * dims + 2 * outputs);
    AlignedBuffer<double> slab(stride * threads);
    std::vector<Moments> chunks(n_chunks, Moments(outputs));

    parallel_for(n_chunks, threads, [&](std::int64_t c, int w) {
        double* u = slab.data() + w * stride;
        double* u_anti = u + dims;
        double* out = u_anti + dims;
        double* out_anti = out + outputs;
        Moments m(outputs);
        const std::int64_t end = std::min(n, (c + 1) * kChunk);
        for (std::int64_t s = c * kChunk; s < end; ++s) {
            path_uniforms(cfg.seed, s, 0, u, dims);
            path_fn(u, out);
            if (cfg.antithetic) {
                for (int i = 0; i < dims; ++i) u_anti[i] = 1.0 - u[i];
                path_fn(u_anti, out_anti);
                for (int j = 0; j < outputs; ++j) out[j] = 0.5 * (out[j] + out_anti[j]);
            }
            m.add(out);
        }
        chunks[c] = m;
    });

    Moments total(outputs);
    for (const auto& m : chunks) total.merge(m);
    return total;
}

Estimate estimate(const Moments& m, int i, double scale) {
    return {scale * m.mean(i), scale * std::sqrt(m.cov(i, i) / m.n)};
}

}  // namespace

McResult price_european_mc(double spot, double strike, double rate, double div_yield, double vol,
                           double maturity, bool is_call, const SimConfig& cfg) {
    check(spot, strike, vol, maturity, cfg);
    const double drift = (rate - div_yield - 0.5 * vol * vol) * maturity;
    const double sqrt_t = std::sqrt(maturity);
    const double sign = is_call ? 1.0 : -1.0;

    // Pathwise Greeks: d payoff / d S0 = 1{ITM} * S_T / S0 and
    // d payoff / d vol = 1{ITM} * S_T * (sqrt(T) z - vol T), with the sign of
    // the payoff. Unbiased because the payoff's kink at K has probability zero.
    const Moments m = simulate(cfg, 1, 3, [&](const double* u, double* out) {
        const double z = inv_norm_cdf(u[0]);
        const double st = spot * std::exp(drift + vol * sqrt_t * z);
        const double intrinsic = sign * (st - strike);
        if (intrinsic <= 0) {
            out[0] = out[1] = out[2] = 0.0;
            return;
        }
        out[0] = intrinsic;
        out[1] = sign * st / spot;
        out[2] = sign * st * (sqrt_t * z - vol * maturity);
    });

    const double df = std::exp(-rate * maturity);
    McResult r;
    r.price = estimate(m, 0, df);
    r.delta = estimate(m, 1, df);
    r.vega = estimate(m, 2, df);
    r.n_paths = cfg.antithetic ? 2 * m.n : m.n;
    return r;
}

McResult price_asian_mc(double spot, double strike, double rate, double div_yield, double vol,
                        double maturity, int n_fixings, bool is_call, const SimConfig& cfg) {
    check(spot, strike, vol, maturity, cfg);
    if (n_fixings < 1 || n_fixings > kMaxFixings) {
        throw std::invalid_argument("n_fixings must be between 1 and 1000");
    }
    const double dt = maturity / n_fixings;
    const double step_drift = (rate - div_yield - 0.5 * vol * vol) * dt;
    const double step_vol = vol * std::sqrt(dt);
    const double log_spot = std::log(spot);
    const double sign = is_call ? 1.0 : -1.0;

    // Outputs: arithmetic-average payoff, geometric-average payoff.
    const Moments m = simulate(cfg, n_fixings, 2, [&](const double* u, double* out) {
        double log_s = log_spot, sum_s = 0.0, sum_log = 0.0;
        for (int i = 0; i < n_fixings; ++i) {
            log_s += step_drift + step_vol * inv_norm_cdf(u[i]);
            sum_s += std::exp(log_s);
            sum_log += log_s;
        }
        out[0] = std::max(sign * (sum_s / n_fixings - strike), 0.0);
        out[1] = std::max(sign * (std::exp(sum_log / n_fixings) - strike), 0.0);
    });

    const double df = std::exp(-rate * maturity);
    McResult r;
    r.n_paths = cfg.antithetic ? 2 * m.n : m.n;
    if (!cfg.control_variate) {
        r.price = estimate(m, 0, df);
        return r;
    }
    // Control variate: price = mean(X) - beta (mean(Y) - E[Y]) with the
    // variance-minimising beta = Cov(X, Y) / Var(Y). The remaining variance
    // is Var(X) (1 - corr^2). Estimating beta from the same paths adds an
    // O(1/n) bias, negligible at these path counts.
    const double var_x = m.cov(0, 0), var_y = m.cov(1, 1), cov_xy = m.cov(0, 1);
    const double beta = var_y > 0 ? cov_xy / var_y : 0.0;
    const double y_mean =
        geometric_asian(spot, strike, rate, div_yield, vol, maturity, n_fixings, is_call) / df;
    const double var_cv = std::max(var_x - beta * cov_xy, 0.0);
    r.price = {df * (m.mean(0) - beta * (m.mean(1) - y_mean)), df * std::sqrt(var_cv / m.n)};
    r.variance_reduction =
        var_cv > 0 ? var_x / var_cv : std::numeric_limits<double>::infinity();
    return r;
}

}  // namespace mc
