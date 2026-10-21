#pragma once

#include <cstdint>
#include <cstdlib>

// Under Valgrind (roughly 30x slower) the suite runs with MC_TEST_FAST=1.
// Statistical assertions compare against standard errors, so they stay valid
// at the smaller path counts.
inline std::int64_t paths(std::int64_t n) {
    return std::getenv("MC_TEST_FAST") ? n / 10 : n;
}
