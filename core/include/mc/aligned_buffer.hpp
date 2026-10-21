#pragma once

#include <cstddef>
#include <limits>
#include <memory>
#include <new>
#include <type_traits>
#include <utility>

namespace mc {

constexpr std::size_t kCacheLine = 64;

// Owning, move-only array aligned to a cache line. Per-thread scratch lives in
// one of these, with each thread's slice padded to whole lines, so two threads
// never write to the same 64-byte line. If they did ("false sharing"), every
// write would invalidate the line in the other core's cache.
template <class T>
class AlignedBuffer {
    static_assert(std::is_trivially_copyable_v<T> && std::is_trivially_destructible_v<T>);

public:
    AlignedBuffer() = default;

    explicit AlignedBuffer(std::size_t n) : size_(n) {
        if (n == 0) return;
        if (n > std::numeric_limits<std::size_t>::max() / sizeof(T)) throw std::bad_array_new_length();
        data_ = static_cast<T*>(::operator new(n * sizeof(T), std::align_val_t{kCacheLine}));
        std::uninitialized_value_construct_n(data_, n);
    }

    ~AlignedBuffer() { release(); }

    AlignedBuffer(const AlignedBuffer&) = delete;
    AlignedBuffer& operator=(const AlignedBuffer&) = delete;

    AlignedBuffer(AlignedBuffer&& other) noexcept
        : data_(std::exchange(other.data_, nullptr)), size_(std::exchange(other.size_, 0)) {}

    AlignedBuffer& operator=(AlignedBuffer&& other) noexcept {
        if (this != &other) {
            release();
            data_ = std::exchange(other.data_, nullptr);
            size_ = std::exchange(other.size_, 0);
        }
        return *this;
    }

    T* data() { return data_; }
    const T* data() const { return data_; }
    std::size_t size() const { return size_; }
    T& operator[](std::size_t i) { return data_[i]; }
    const T& operator[](std::size_t i) const { return data_[i]; }

private:
    void release() {
        if (data_) ::operator delete(data_, std::align_val_t{kCacheLine});
        data_ = nullptr;
    }

    T* data_ = nullptr;
    std::size_t size_ = 0;
};

// Elements per thread, rounded up to whole cache lines.
template <class T>
constexpr std::size_t cache_line_stride(std::size_t n) {
    constexpr std::size_t per_line = kCacheLine / sizeof(T);
    return (n + per_line - 1) / per_line * per_line;
}

}  // namespace mc
