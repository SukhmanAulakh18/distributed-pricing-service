#include "mc/exposure.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <stdexcept>

#include "mc/aligned_buffer.hpp"
#include "mc/analytic.hpp"
#include "mc/parallel.hpp"
#include "mc/random.hpp"

namespace mc {
namespace {

constexpr std::int64_t kChunk = 1024;
constexpr double kMaxTailElements = 2.5e8;  // 2 GB of doubles across all threads

double trade_value(const ExposureTrade& t, const Underlying& u, double spot, double now,
                   double rate) {
    const double tau = t.maturity - now;
    // On the maturity date itself the payoff is still owed (it settles after
    // the node), so it counts at intrinsic value; after that it is gone.
    if (tau < -1e-12) return 0.0;
    if (tau <= 1e-12) {
        const double intrinsic = t.kind == TradeKind::Call  ? std::max(spot - t.strike, 0.0)
                                 : t.kind == TradeKind::Put ? std::max(t.strike - spot, 0.0)
                                                            : spot - t.strike;
        return t.quantity * intrinsic;
    }
    if (t.kind == TradeKind::Forward) {
        return t.quantity * (spot * std::exp(-u.div_yield * tau) - t.strike * std::exp(-rate * tau));
    }
    return t.quantity * bs_price_unchecked(spot, t.strike, rate, u.div_yield, u.vol, tau,
                                           t.kind == TradeKind::Call);
}

// Keeps the k largest values seen in a min-heap. Capacity is reserved up
// front, so this never allocates.
void push_top_k(std::vector<double>& heap, int k, double x) {
    if (static_cast<int>(heap.size()) < k) {
        heap.push_back(x);
        std::push_heap(heap.begin(), heap.end(), std::greater<>());
    } else if (x > heap.front()) {
        std::pop_heap(heap.begin(), heap.end(), std::greater<>());
        heap.back() = x;
        std::push_heap(heap.begin(), heap.end(), std::greater<>());
    }
}

void validate(const std::vector<Underlying>& underlyings, const std::vector<ExposureTrade>& trades,
              const std::vector<double>& grid, std::int64_t start, std::int64_t count, int tail_k,
              int threads) {
    if (underlyings.empty()) throw std::invalid_argument("need at least one underlying");
    for (const auto& u : underlyings) {
        if (!(u.spot > 0) || !(u.vol > 0)) throw std::invalid_argument("spot and vol must be positive");
    }
    for (const auto& t : trades) {
        if (t.underlying < 0 || t.underlying >= static_cast<int>(underlyings.size())) {
            throw std::invalid_argument("trade references an unknown underlying");
        }
        if (!(t.strike > 0) || !(t.maturity > 0) || !std::isfinite(t.quantity)) {
            throw std::invalid_argument("trades need positive strike and maturity and a finite quantity");
        }
    }
    if (grid.empty()) throw std::invalid_argument("grid must not be empty");
    for (std::size_t j = 0; j < grid.size(); ++j) {
        if (!(grid[j] > (j ? grid[j - 1] : 0.0))) {
            throw std::invalid_argument("grid must be positive and strictly increasing");
        }
    }
    if (start < 0 || count < 0 || tail_k < 0 || threads < 1) {
        throw std::invalid_argument("start, count and tail_k must be non-negative, threads positive");
    }
}

void validate_csa(const CsaTerms& csa, std::size_t steps) {
    if (!(csa.threshold_recv >= 0) || !(csa.threshold_post >= 0) || !(csa.mta >= 0) ||
        !(csa.independent_amount >= 0) || std::isnan(csa.threshold_recv + csa.threshold_post)) {
        throw std::invalid_argument("CSA thresholds, MTA and independent amount must be non-negative");
    }
    if (csa.lag_index.size() != steps) throw std::invalid_argument("CSA lag_index needs one entry per step");
    for (std::size_t j = 0; j < steps; ++j) {
        if (csa.lag_index[j] < -1 || csa.lag_index[j] > static_cast<int>(j)) {
            throw std::invalid_argument("CSA lag_index[j] must be in [-1, j]");
        }
    }
}

}  // namespace

double csa_target(double v, const CsaTerms& csa) {
    return std::max(v - csa.threshold_recv, 0.0) - std::max(-v - csa.threshold_post, 0.0);
}

std::vector<double> cholesky(const std::vector<double>& a, int n) {
    if (n < 1 || a.size() != static_cast<std::size_t>(n) * n) {
        throw std::invalid_argument("correlation must be an n x n matrix");
    }
    std::vector<double> l(a.size(), 0.0);
    for (int i = 0; i < n; ++i) {
        if (std::abs(a[i * n + i] - 1.0) > 1e-12) {
            throw std::invalid_argument("correlation diagonal must be 1");
        }
        for (int j = 0; j <= i; ++j) {
            if (std::abs(a[i * n + j] - a[j * n + i]) > 1e-12) {
                throw std::invalid_argument("correlation must be symmetric");
            }
            double s = a[i * n + j];
            for (int k = 0; k < j; ++k) s -= l[i * n + k] * l[j * n + k];
            if (i == j) {
                if (!(s > 1e-12)) throw std::invalid_argument("correlation is not positive definite");
                l[i * n + i] = std::sqrt(s);
            } else {
                l[i * n + j] = s / l[j * n + j];
            }
        }
    }
    return l;
}

ExposureBlock simulate_exposure(const std::vector<Underlying>& underlyings,
                                const std::vector<double>& correlation,
                                const std::vector<ExposureTrade>& trades,
                                const std::vector<double>& grid, double rate, std::uint64_t seed,
                                std::int64_t start, std::int64_t count, int tail_k, int threads) {
    return simulate_exposure(underlyings, correlation, trades, grid, rate, seed, start, count, tail_k,
                             threads, nullptr);
}

ExposureBlock simulate_exposure(const std::vector<Underlying>& underlyings,
                                const std::vector<double>& correlation,
                                const std::vector<ExposureTrade>& trades,
                                const std::vector<double>& grid, double rate, std::uint64_t seed,
                                std::int64_t start, std::int64_t count, int tail_k, int threads,
                                const CsaTerms* csa) {
    validate(underlyings, trades, grid, start, count, tail_k, threads);
    if (csa) validate_csa(*csa, grid.size());
    const int n_series = csa ? 2 : 1;  // uncollateralized, then collateralized
    const int n_u = static_cast<int>(underlyings.size());
    const int steps = static_cast<int>(grid.size());
    const std::vector<double> chol = cholesky(correlation, n_u);
    const int k = static_cast<int>(std::min<std::int64_t>(tail_k, count));

    const std::int64_t n_chunks = (count + kChunk - 1) / kChunk;
    threads = effective_threads(n_chunks, threads);
    if (static_cast<double>(threads) * steps * k * n_series > kMaxTailElements) {
        throw std::invalid_argument("tail buffer too large: reduce tail_k, steps or threads");
    }

    // Per-step, per-underlying GBM increments: drift and vol * sqrt(dt).
    std::vector<double> drift(steps * n_u), diffusion(steps * n_u);
    for (int j = 0; j < steps; ++j) {
        const double dt = grid[j] - (j ? grid[j - 1] : 0.0);
        for (int a = 0; a < n_u; ++a) {
            const auto& u = underlyings[a];
            drift[j * n_u + a] = (rate - u.div_yield - 0.5 * u.vol * u.vol) * dt;
            diffusion[j * n_u + a] = u.vol * std::sqrt(dt);
        }
    }

    double v0 = 0.0;  // today's value: deterministic, and the collateral balance at t = 0
    for (const auto& t : trades) v0 += trade_value(t, underlyings[t.underlying], underlyings[t.underlying].spot, 0.0, rate);

    // Per chunk and step: sum, sum of squares, negative sum; then the same
    // three for the collateralized series.
    const int n_acc = 3 * n_series;
    std::vector<double> chunk_sums(n_chunks * steps * n_acc, 0.0);
    // Per thread: uniforms, normals, log-spots, spots, then the collateral
    // balance at each step of the current scenario. Reserved once, so no
    // allocation per path.
    const std::size_t stride = cache_line_stride<double>(4 * n_u + (csa ? steps : 0));
    AlignedBuffer<double> slab(stride * threads);
    std::vector<std::vector<double>> heaps(static_cast<std::size_t>(threads) * steps * n_series);
    for (auto& h : heaps) h.reserve(k);
    const double balance0 = csa ? csa_target(v0, *csa) : 0.0;

    parallel_for(n_chunks, threads, [&](std::int64_t c, int w) {
        double* u = slab.data() + w * stride;
        double* z = u + n_u;
        double* log_s = z + n_u;
        double* spot = log_s + n_u;
        double* balance = spot + n_u;
        double* sums = chunk_sums.data() + c * steps * n_acc;
        const std::int64_t end = std::min(count, (c + 1) * kChunk);
        for (std::int64_t s = c * kChunk; s < end; ++s) {
            for (int a = 0; a < n_u; ++a) log_s[a] = std::log(underlyings[a].spot);
            for (int j = 0; j < steps; ++j) {
                // Stream j + 1: each time step draws its own normals for this scenario.
                path_uniforms(seed, start + s, static_cast<std::uint32_t>(j + 1), u, n_u);
                for (int a = 0; a < n_u; ++a) z[a] = inv_norm_cdf(u[a]);
                for (int a = 0; a < n_u; ++a) {
                    double y = 0.0;  // row a of L z: correlated shock
                    for (int b = 0; b <= a; ++b) y += chol[a * n_u + b] * z[b];
                    log_s[a] += drift[j * n_u + a] + diffusion[j * n_u + a] * y;
                    spot[a] = std::exp(log_s[a]);
                }
                double value = 0.0;
                for (const auto& t : trades) {
                    value += trade_value(t, underlyings[t.underlying], spot[t.underlying], grid[j], rate);
                }
                const double e = std::max(value, 0.0);
                double* acc = sums + j * n_acc;
                acc[0] += e;
                acc[1] += e * e;
                acc[2] += std::max(-value, 0.0);
                if (k > 0) push_top_k(heaps[(static_cast<std::size_t>(w) * n_series) * steps + j], k, e);
                if (csa) {
                    // Margin call at this node, made only if it moves the balance by the MTA.
                    const double prev = j ? balance[j - 1] : balance0;
                    const double target = csa_target(value, *csa);
                    balance[j] = std::abs(target - prev) >= csa->mta ? target : prev;
                    const int lag = csa->lag_index[j];
                    const double held = lag < 0 ? balance0 : balance[lag];
                    const double ec = std::max(value - held - csa->independent_amount, 0.0);
                    acc[3] += ec;
                    acc[4] += ec * ec;
                    acc[5] += std::max(held - value, 0.0);
                    if (k > 0) push_top_k(heaps[(static_cast<std::size_t>(w) * n_series + 1) * steps + j], k, ec);
                }
            }
        }
    });

    ExposureBlock out;
    out.n = count;
    out.tail_k = k;
    out.v0 = v0;
    std::vector<double>* sum_out[2][3] = {{&out.sum, &out.sum_sq, &out.neg_sum},
                                          {&out.csa_sum, &out.csa_sum_sq, &out.csa_neg_sum}};
    std::vector<double>* tail_out[2] = {&out.tails, &out.csa_tails};
    std::vector<double> merged;
    merged.reserve(static_cast<std::size_t>(threads) * k);
    for (int series = 0; series < n_series; ++series) {
        for (int a = 0; a < 3; ++a) sum_out[series][a]->assign(steps, 0.0);
        for (std::int64_t c = 0; c < n_chunks; ++c) {  // chunk order: deterministic
            for (int j = 0; j < steps; ++j) {
                for (int a = 0; a < 3; ++a) {
                    (*sum_out[series][a])[j] += chunk_sums[(c * steps + j) * n_acc + 3 * series + a];
                }
            }
        }
        auto& tails = *tail_out[series];
        tails.resize(static_cast<std::size_t>(steps) * k);
        for (int j = 0; j < steps && k > 0; ++j) {
            merged.clear();
            for (int w = 0; w < threads; ++w) {
                const auto& h = heaps[(static_cast<std::size_t>(w) * n_series + series) * steps + j];
                merged.insert(merged.end(), h.begin(), h.end());
            }
            std::partial_sort(merged.begin(), merged.begin() + k, merged.end(), std::greater<>());
            std::copy(merged.begin(), merged.begin() + k, tails.begin() + static_cast<std::ptrdiff_t>(j) * k);
        }
    }
    return out;
}

}  // namespace mc
