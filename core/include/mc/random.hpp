#pragma once

#include <array>
#include <cmath>
#include <cstdint>

namespace mc {

// Philox4x32-10 (Salmon et al., "Parallel Random Numbers: As Easy as 1, 2, 3",
// SC11). Counter-based: the output is a pure function of (counter, key), so a
// path's random numbers come straight from its index. Nothing is carried
// between paths, which is what makes results independent of thread count,
// identical on every worker, and identical across compilers and OSes (unlike
// std::normal_distribution, which differs between libc++ and libstdc++).
struct Philox4x32 {
    using Counter = std::array<std::uint32_t, 4>;
    using Key = std::array<std::uint32_t, 2>;

    static constexpr Counter generate(Counter c, Key k) {
        for (int round = 0; round < 10; ++round) {
            if (round > 0) {
                k[0] += 0x9E3779B9u;
                k[1] += 0xBB67AE85u;
            }
            const std::uint64_t p0 = std::uint64_t{0xD2511F53u} * c[0];
            const std::uint64_t p1 = std::uint64_t{0xCD9E8D57u} * c[2];
            c = {static_cast<std::uint32_t>(p1 >> 32) ^ c[1] ^ k[0], static_cast<std::uint32_t>(p1),
                 static_cast<std::uint32_t>(p0 >> 32) ^ c[3] ^ k[1], static_cast<std::uint32_t>(p0)};
        }
        return c;
    }
};

// Midpoint of the integer's cell in (0, 1): never 0 or 1, so the inverse
// normal stays finite, and 1 - u is exact, so antithetic pairs are exact.
inline double to_uniform(std::uint32_t x) {
    return (static_cast<double>(x) + 0.5) * 0x1p-32;
}

// Uniforms for one path or scenario. `stream` separates independent uses of
// the same index (e.g. one stream per time step).
inline void path_uniforms(std::uint64_t seed, std::uint64_t index, std::uint32_t stream,
                          double* u, int n) {
    const Philox4x32::Key key{static_cast<std::uint32_t>(seed),
                              static_cast<std::uint32_t>(seed >> 32)};
    for (int block = 0; 4 * block < n; ++block) {
        const auto r = Philox4x32::generate(
            {static_cast<std::uint32_t>(index), static_cast<std::uint32_t>(index >> 32),
             static_cast<std::uint32_t>(block), stream},
            key);
        for (int j = 0; j < 4 && 4 * block + j < n; ++j) u[4 * block + j] = to_uniform(r[j]);
    }
}

inline double norm_pdf(double x) {
    return 0.3989422804014327 * std::exp(-0.5 * x * x);
}

inline double norm_cdf(double x) {
    return 0.5 * std::erfc(-x * 0.7071067811865476);
}

// Inverse standard normal CDF: Acklam's rational approximation, relative
// error below 1.15e-9 (far under Monte Carlo noise). The tail branch runs on
// min(p, 1 - p), so inv_norm_cdf(1 - p) == -inv_norm_cdf(p) exactly.
inline double inv_norm_cdf(double p) {
    constexpr double a[] = {-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
                            1.383577518672690e+02,  -3.066479806614716e+01, 2.506628277459239e+00};
    constexpr double b[] = {-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
                            6.680131188771972e+01,  -1.328068155288572e+01};
    constexpr double c[] = {-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
                            -2.549732539343734e+00, 4.374664141464968e+00,  2.938163982698783e+00};
    constexpr double d[] = {7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
                            3.754408661907416e+00};
    constexpr double p_low = 0.02425;

    auto tail = [&](double q_in) {
        const double q = std::sqrt(-2.0 * std::log(q_in));
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) /
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0);
    };
    if (p < p_low) return tail(p);
    if (p > 1.0 - p_low) return -tail(1.0 - p);
    const double q = p - 0.5, r = q * q;
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q /
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0);
}

}  // namespace mc
