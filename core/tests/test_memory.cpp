#include <gtest/gtest.h>

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <new>
#include <utility>

#include "mc/aligned_buffer.hpp"
#include "mc/pricing.hpp"

// Count every heap allocation in this test binary by replacing the global
// allocation functions. This proves the pricing hot loop is allocation-free:
// the number of allocations must not grow with the number of paths.
//
// AddressSanitizer owns operator new itself (and checks allocations its own
// way), so under ASan the replacement is compiled out and the test skips.
#if defined(__SANITIZE_ADDRESS__)
#define MC_COUNT_ALLOCATIONS 0
#elif defined(__has_feature)
#if __has_feature(address_sanitizer)
#define MC_COUNT_ALLOCATIONS 0
#endif
#endif
#ifndef MC_COUNT_ALLOCATIONS
#define MC_COUNT_ALLOCATIONS 1
#endif

namespace {
std::atomic<std::int64_t> g_allocations{0};

void* counted_alloc(std::size_t size, std::size_t align) {
    g_allocations.fetch_add(1, std::memory_order_relaxed);
    void* p = nullptr;
    if (posix_memalign(&p, std::max(align, sizeof(void*)), size ? size : 1) != 0) throw std::bad_alloc();
    return p;
}
}  // namespace

#if MC_COUNT_ALLOCATIONS
void* operator new(std::size_t size) { return counted_alloc(size, alignof(std::max_align_t)); }
void* operator new[](std::size_t size) { return counted_alloc(size, alignof(std::max_align_t)); }
void* operator new(std::size_t size, std::align_val_t a) { return counted_alloc(size, static_cast<std::size_t>(a)); }
void* operator new[](std::size_t size, std::align_val_t a) { return counted_alloc(size, static_cast<std::size_t>(a)); }
void operator delete(void* p) noexcept { std::free(p); }
void operator delete[](void* p) noexcept { std::free(p); }
void operator delete(void* p, std::size_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t) noexcept { std::free(p); }
void operator delete(void* p, std::align_val_t) noexcept { std::free(p); }
void operator delete[](void* p, std::align_val_t) noexcept { std::free(p); }
void operator delete(void* p, std::size_t, std::align_val_t) noexcept { std::free(p); }
void operator delete[](void* p, std::size_t, std::align_val_t) noexcept { std::free(p); }
#endif

namespace {
template <class F>
std::int64_t allocations_during(F&& f) {
    const auto before = g_allocations.load();
    f();
    return g_allocations.load() - before;
}
}  // namespace

TEST(Memory, PricingAllocationsDoNotGrowWithPathCount) {
    if (!MC_COUNT_ALLOCATIONS) GTEST_SKIP() << "ASan replaces operator new";
    auto run = [](std::int64_t n) {
        return allocations_during([&] {
            mc::price_asian_mc(100, 100, 0.03, 0, 0.2, 1, 12, true, {n, true, true, 1, 4});
        });
    };
    const auto small = run(100'000), large = run(1'000'000);
    EXPECT_EQ(small, large) << "the hot loop allocates per path";
    EXPECT_LT(large, 32);  // scratch slab, chunk table, thread handles
}

TEST(AlignedBuffer, IsCacheLineAlignedAndZeroed) {
    mc::AlignedBuffer<double> b(1000);
    EXPECT_EQ(reinterpret_cast<std::uintptr_t>(b.data()) % mc::kCacheLine, 0u);
    for (std::size_t i = 0; i < b.size(); ++i) EXPECT_EQ(b[i], 0.0);
}

TEST(AlignedBuffer, MoveTransfersOwnership) {
    mc::AlignedBuffer<double> a(16);
    a[3] = 42.0;
    double* raw = a.data();
    mc::AlignedBuffer<double> b(std::move(a));
    EXPECT_EQ(b.data(), raw);
    EXPECT_EQ(b[3], 42.0);
    EXPECT_EQ(a.data(), nullptr);
    EXPECT_EQ(a.size(), 0u);
    mc::AlignedBuffer<double> c(8);
    c = std::move(b);  // frees c's old buffer, takes b's
    EXPECT_EQ(c.data(), raw);
    EXPECT_EQ(b.data(), nullptr);
    c = std::move(c);  // self-move is a no-op, not a double free
    EXPECT_EQ(c.data(), raw);
}

TEST(AlignedBuffer, StridePadsToWholeCacheLines) {
    EXPECT_EQ(mc::cache_line_stride<double>(1), 8u);
    EXPECT_EQ(mc::cache_line_stride<double>(8), 8u);
    EXPECT_EQ(mc::cache_line_stride<double>(9), 16u);
}
