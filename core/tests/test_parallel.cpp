#include <gtest/gtest.h>

#include <atomic>
#include <stdexcept>
#include <vector>

#include "mc/parallel.hpp"

TEST(ParallelFor, RunsEveryTaskExactlyOnce) {
    std::vector<std::atomic<int>> hits(10'000);
    mc::parallel_for(10'000, 8, [&](std::int64_t i, int) { hits[i].fetch_add(1); });
    for (const auto& h : hits) EXPECT_EQ(h.load(), 1);
}

TEST(ParallelFor, WorkerIndexStaysInRange) {
    std::atomic<int> bad{0};
    mc::parallel_for(1'000, 4, [&](std::int64_t, int w) {
        if (w < 0 || w >= 4) bad.fetch_add(1);
    });
    EXPECT_EQ(bad.load(), 0);
}

TEST(ParallelFor, RethrowsTaskExceptionsInsteadOfTerminating) {
    EXPECT_THROW(mc::parallel_for(1'000, 4,
                                  [](std::int64_t i, int) {
                                      if (i == 500) throw std::runtime_error("boom");
                                  }),
                 std::runtime_error);
}

TEST(ParallelFor, HandlesMoreThreadsThanTasksAndEmptyRanges) {
    std::atomic<int> count{0};
    mc::parallel_for(3, 64, [&](std::int64_t, int) { count.fetch_add(1); });
    EXPECT_EQ(count.load(), 3);
    mc::parallel_for(0, 8, [&](std::int64_t, int) { count.fetch_add(1); });
    EXPECT_EQ(count.load(), 3);
    EXPECT_EQ(mc::effective_threads(5, 64), 5);
    EXPECT_EQ(mc::effective_threads(100, 0), 1);
}
