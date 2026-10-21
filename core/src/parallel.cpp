#include "mc/parallel.hpp"

#include <algorithm>
#include <atomic>
#include <exception>
#include <mutex>
#include <thread>
#include <vector>

namespace mc {

int effective_threads(std::int64_t n, int threads) {
    return static_cast<int>(std::clamp<std::int64_t>(threads, 1, std::max<std::int64_t>(n, 1)));
}

void parallel_for(std::int64_t n, int threads,
                  const std::function<void(std::int64_t, int)>& task) {
    threads = effective_threads(n, threads);
    std::atomic<std::int64_t> next{0};
    std::atomic<bool> failed{false};
    std::exception_ptr error;
    std::mutex error_mutex;

    auto worker = [&](int w) {
        for (std::int64_t i = next.fetch_add(1); i < n && !failed.load(); i = next.fetch_add(1)) {
            try {
                task(i, w);
            } catch (...) {
                std::lock_guard lock(error_mutex);
                if (!error) error = std::current_exception();
                failed = true;
            }
        }
    };

    std::vector<std::thread> pool;
    pool.reserve(threads - 1);
    try {
        for (int w = 1; w < threads; ++w) pool.emplace_back(worker, w);
    } catch (...) {
        // Thread creation can fail under container PID or memory limits.
        // Join whatever started before propagating, or ~thread terminates.
        failed = true;
        for (auto& t : pool) t.join();
        throw;
    }
    worker(0);
    for (auto& t : pool) t.join();
    if (error) std::rethrow_exception(error);
}

}  // namespace mc
