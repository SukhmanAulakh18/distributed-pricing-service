#pragma once

#include <cstdint>
#include <functional>

namespace mc {

// Clamps a requested thread count to [1, n].
int effective_threads(std::int64_t n, int threads);

// Runs task(i, worker) for every i in [0, n) on up to `threads` threads, the
// calling thread included. `worker` is in [0, threads) and indexes per-thread
// scratch. If a task throws, remaining tasks are skipped and the first
// exception is rethrown here once every thread has joined. An exception
// escaping a std::thread would otherwise call std::terminate and kill the
// whole Python process.
void parallel_for(std::int64_t n, int threads,
                  const std::function<void(std::int64_t, int)>& task);

}  // namespace mc
