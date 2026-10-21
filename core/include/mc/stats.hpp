#pragma once

#include <array>
#include <cstdint>
#include <limits>
#include <utility>

namespace mc {

constexpr int kMaxOutputs = 4;

// Running sums for up to kMaxOutputs values observed together on each path:
// enough for every mean, variance and pairwise covariance (the control-variate
// estimator needs the covariance). Fixed size, so accumulating never allocates.
struct Moments {
    int k = 0;
    std::int64_t n = 0;
    std::array<double, kMaxOutputs> sum{};
    std::array<double, kMaxOutputs * kMaxOutputs> cross{};

    explicit Moments(int outputs = 0) : k(outputs) {}

    void add(const double* x) {
        ++n;
        for (int i = 0; i < k; ++i) {
            sum[i] += x[i];
            for (int j = i; j < k; ++j) cross[i * kMaxOutputs + j] += x[i] * x[j];
        }
    }

    void merge(const Moments& o) {
        n += o.n;
        for (int i = 0; i < kMaxOutputs; ++i) sum[i] += o.sum[i];
        for (int i = 0; i < kMaxOutputs * kMaxOutputs; ++i) cross[i] += o.cross[i];
    }

    double mean(int i) const { return sum[i] / n; }

    double cov(int i, int j) const {
        if (i > j) std::swap(i, j);
        return (cross[i * kMaxOutputs + j] - sum[i] * sum[j] / n) / (n - 1);
    }
};

struct Estimate {
    double value = std::numeric_limits<double>::quiet_NaN();
    double std_err = std::numeric_limits<double>::quiet_NaN();
};

}  // namespace mc
