#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>

#include "mc/random.hpp"
#include "test_util.hpp"

using mc::Philox4x32;

// Known-answer vectors from the Random123 distribution (kat_vectors).
TEST(Philox, KnownAnswerVectors) {
    EXPECT_EQ(Philox4x32::generate({0, 0, 0, 0}, {0, 0}),
              (Philox4x32::Counter{0x6627e8d5, 0xe169c58d, 0xbc57ac4c, 0x9b00dbd8}));
    EXPECT_EQ(Philox4x32::generate({0xffffffff, 0xffffffff, 0xffffffff, 0xffffffff},
                                   {0xffffffff, 0xffffffff}),
              (Philox4x32::Counter{0x408f276d, 0x41c83b0e, 0xa20bc7c6, 0x6d5451fd}));
    EXPECT_EQ(Philox4x32::generate({0x243f6a88, 0x85a308d3, 0x13198a2e, 0x03707344},
                                   {0xa4093822, 0x299f31d0}),
              (Philox4x32::Counter{0xd16cfe09, 0x94fdcceb, 0x5001e420, 0x24126ea1}));
}

TEST(Uniform, StrictlyInsideUnitIntervalAndAntitheticExact) {
    EXPECT_GT(mc::to_uniform(0), 0.0);
    EXPECT_LT(mc::to_uniform(0xffffffffu), 1.0);
    for (std::uint32_t x : {0u, 1u, 12345u, 0x7fffffffu, 0xfffffffeu}) {
        EXPECT_EQ(1.0 - mc::to_uniform(x), mc::to_uniform(0xffffffffu - x));
    }
}

TEST(Uniform, SameIndexSameNumbersDifferentIndexDifferent) {
    double a[6], b[6], c[6];
    mc::path_uniforms(7, 1000, 0, a, 6);
    mc::path_uniforms(7, 1000, 0, b, 6);
    mc::path_uniforms(7, 1001, 0, c, 6);
    for (int i = 0; i < 6; ++i) {
        EXPECT_EQ(a[i], b[i]);
        EXPECT_NE(a[i], c[i]);
    }
}

// Reference inverse: Newton on the exact CDF (erfc) to machine precision.
static double inv_norm_reference(double p) {
    double x = mc::inv_norm_cdf(p);
    for (int i = 0; i < 3; ++i) x -= (mc::norm_cdf(x) - p) / mc::norm_pdf(x);
    return x;
}

TEST(InverseNormal, AccurateAcrossBodyAndTails) {
    for (double p : {1e-12, 1e-8, 1e-4, 0.02, 0.02425, 0.1, 0.3, 0.5, 0.7, 0.97575, 0.99, 1 - 1e-6}) {
        const double ref = inv_norm_reference(p);
        EXPECT_NEAR(mc::inv_norm_cdf(p), ref, 2e-9 * std::max(1.0, std::abs(ref))) << "p=" << p;
    }
}

TEST(InverseNormal, ExactlySymmetricOnTheUniformGrid) {
    for (std::uint32_t x : {0u, 77u, 104000000u, 0x7fffffffu}) {
        const double u = mc::to_uniform(x);
        EXPECT_EQ(mc::inv_norm_cdf(1.0 - u), -mc::inv_norm_cdf(u));
    }
}

TEST(InverseNormal, PhiloxNormalsHaveUnitMoments) {
    const std::int64_t n = paths(1'000'000);
    double sum = 0, sum_sq = 0, u[4];
    for (std::int64_t i = 0; i < n / 4; ++i) {
        mc::path_uniforms(123, i, 0, u, 4);
        for (double x : u) {
            const double z = mc::inv_norm_cdf(x);
            sum += z;
            sum_sq += z * z;
        }
    }
    const double count = 4.0 * (n / 4), mean = sum / count, var = sum_sq / count - mean * mean;
    EXPECT_LT(std::abs(mean), 5 / std::sqrt(count));
    EXPECT_LT(std::abs(var - 1), 5 * std::sqrt(2 / count));
}
