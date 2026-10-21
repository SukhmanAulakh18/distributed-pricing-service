#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "mc/analytic.hpp"
#include "mc/exposure.hpp"
#include "mc/parallel.hpp"
#include "mc/pricing.hpp"

namespace py = pybind11;
using namespace py::literals;

namespace {

template <class T>
using Array = py::array_t<T, py::array::c_style | py::array::forcecast>;

// Hands a vector's heap buffer to NumPy without copying. The capsule owns the
// vector and deletes it when the array is garbage-collected. The unique_ptr
// covers the window before the capsule exists, so a throw there cannot leak.
template <class T>
py::array_t<T> to_numpy(std::vector<T>&& v, std::vector<py::ssize_t> shape) {
    auto owned = std::make_unique<std::vector<T>>(std::move(v));
    T* data = owned->data();
    py::capsule owner(owned.get(), [](void* p) { delete static_cast<std::vector<T>*>(p); });
    owned.release();
    return py::array_t<T>(shape, data, owner);
}

const double* checked(const Array<double>& a, py::ssize_t n, const char* name) {
    if (a.ndim() != 1 || a.size() != n) {
        throw std::invalid_argument(std::string(name) + " must be a 1-D array of the same length as spot");
    }
    return a.data();
}

mc::McResult european(double spot, double strike, double rate, double vol, double maturity,
                      bool is_call, double div_yield, std::int64_t n_paths, bool antithetic,
                      std::uint64_t seed, int threads) {
    return mc::price_european_mc(spot, strike, rate, div_yield, vol, maturity, is_call,
                                 {n_paths, antithetic, false, seed, threads});
}

}  // namespace

// mod_gil_not_used: the module keeps no global mutable state, so it is safe on
// free-threaded CPython (3.13t+) too.
PYBIND11_MODULE(pricer_core, m, py::mod_gil_not_used()) {
    m.doc() = "Monte Carlo and closed-form option analytics (C++17/20, pybind11)";

    py::class_<mc::Estimate>(m, "Estimate")
        .def_readonly("value", &mc::Estimate::value)
        .def_readonly("std_err", &mc::Estimate::std_err)
        .def("__repr__", [](const mc::Estimate& e) {
            return "Estimate(value=" + std::to_string(e.value) + ", std_err=" + std::to_string(e.std_err) + ")";
        });

    py::class_<mc::McResult>(m, "McResult")
        .def_readonly("price", &mc::McResult::price)
        .def_readonly("delta", &mc::McResult::delta)
        .def_readonly("vega", &mc::McResult::vega)
        .def_readonly("variance_reduction", &mc::McResult::variance_reduction)
        .def_readonly("n_paths", &mc::McResult::n_paths);

    py::class_<mc::BsResult>(m, "BsResult")
        .def_readonly("price", &mc::BsResult::price)
        .def_readonly("delta", &mc::BsResult::delta)
        .def_readonly("gamma", &mc::BsResult::gamma)
        .def_readonly("vega", &mc::BsResult::vega);

    m.def("black_scholes", &mc::black_scholes, "spot"_a, "strike"_a, "rate"_a, "div_yield"_a,
          "vol"_a, "maturity"_a, "is_call"_a = true);

    m.def("geometric_asian", &mc::geometric_asian, "spot"_a, "strike"_a, "rate"_a,
          "div_yield"_a, "vol"_a, "maturity"_a, "n_fixings"_a, "is_call"_a = true);

    // pybind11 converts every argument while holding the GIL, then the
    // call_guard releases it for the C++ call only. The pricer touches no
    // Python objects, so other Python threads run (and price) meanwhile.
    m.def("price_european_mc", &european, "spot"_a, "strike"_a, "rate"_a, "vol"_a, "maturity"_a,
          "is_call"_a = true, "div_yield"_a = 0.0, "n_paths"_a = 100'000, "antithetic"_a = true,
          "seed"_a = 42, "threads"_a = 1, py::call_guard<py::gil_scoped_release>());

    // Same function with the GIL held throughout. Exists only so the benchmark
    // can measure what releasing the GIL buys.
    m.def("_price_european_mc_holding_gil", &european, "spot"_a, "strike"_a, "rate"_a, "vol"_a,
          "maturity"_a, "is_call"_a = true, "div_yield"_a = 0.0, "n_paths"_a = 100'000,
          "antithetic"_a = true, "seed"_a = 42, "threads"_a = 1);

    m.def(
        "price_asian_mc",
        [](double spot, double strike, double rate, double vol, double maturity, int n_fixings,
           bool is_call, double div_yield, std::int64_t n_paths, bool antithetic,
           bool control_variate, std::uint64_t seed, int threads) {
            return mc::price_asian_mc(spot, strike, rate, div_yield, vol, maturity, n_fixings,
                                      is_call, {n_paths, antithetic, control_variate, seed, threads});
        },
        "spot"_a, "strike"_a, "rate"_a, "vol"_a, "maturity"_a, "n_fixings"_a = 12,
        "is_call"_a = true, "div_yield"_a = 0.0, "n_paths"_a = 100'000, "antithetic"_a = true,
        "control_variate"_a = true, "seed"_a = 42, "threads"_a = 1,
        py::call_guard<py::gil_scoped_release>());

    m.def(
        "bs_batch",
        [](const Array<double>& spot, const Array<double>& strike, double rate,
           const Array<double>& div_yield, const Array<double>& vol,
           const Array<double>& maturity, const Array<bool>& is_call, int threads) {
            // With the GIL held: validate shapes, get input pointers, and
            // allocate the outputs as NumPy arrays so results land in them
            // directly (no copy on return).
            const py::ssize_t n = spot.size();
            if (spot.ndim() != 1) throw std::invalid_argument("spot must be a 1-D array");
            const double* s = spot.data();
            const double* k = checked(strike, n, "strike");
            const double* q = checked(div_yield, n, "div_yield");
            const double* v = checked(vol, n, "vol");
            const double* t = checked(maturity, n, "maturity");
            if (is_call.ndim() != 1 || is_call.size() != n) {
                throw std::invalid_argument("is_call must be a 1-D array of the same length as spot");
            }
            const bool* c = is_call.data();
            py::array_t<double> price(n), delta(n), gamma(n), vega(n);
            double* p = price.mutable_data();
            double* d = delta.mutable_data();
            double* g = gamma.mutable_data();
            double* ve = vega.mutable_data();
            {
                // The raw pointers stay valid without the GIL: the arrays are
                // referenced by this frame until the function returns.
                py::gil_scoped_release release;
                constexpr py::ssize_t kBlock = 4096;
                mc::parallel_for((n + kBlock - 1) / kBlock, threads, [&](std::int64_t b, int) {
                    const py::ssize_t end = std::min<py::ssize_t>(n, (b + 1) * kBlock);
                    for (py::ssize_t i = b * kBlock; i < end; ++i) {
                        if (!(s[i] > 0) || !(k[i] > 0) || !(v[i] > 0) || !(t[i] > 0)) {
                            // One bad row should not fail a whole batch.
                            p[i] = d[i] = g[i] = ve[i] = std::numeric_limits<double>::quiet_NaN();
                            continue;
                        }
                        const auto r = mc::black_scholes(s[i], k[i], rate, q[i], v[i], t[i], c[i]);
                        p[i] = r.price;
                        d[i] = r.delta;
                        g[i] = r.gamma;
                        ve[i] = r.vega;
                    }
                });
            }
            return py::dict("price"_a = price, "delta"_a = delta, "gamma"_a = gamma, "vega"_a = vega);
        },
        "spot"_a, "strike"_a, "rate"_a, "div_yield"_a, "vol"_a, "maturity"_a, "is_call"_a,
        "threads"_a = 1);

    m.def(
        "simulate_exposure",
        [](const Array<double>& spot, const Array<double>& vol, const Array<double>& div_yield,
           const Array<double>& correlation, const Array<int>& trade_underlying,
           const Array<int>& trade_kind, const Array<double>& strike,
           const Array<double>& maturity, const Array<double>& quantity,
           const Array<double>& grid, double rate, std::uint64_t seed, std::int64_t start,
           std::int64_t count, int tail_k, int threads) {
            // Copy the (small) inputs into C++ structs while holding the GIL.
            const py::ssize_t n_u = spot.size(), n_t = trade_underlying.size();
            std::vector<mc::Underlying> und(n_u);
            const double* vv = checked(vol, n_u, "vol");
            const double* qq = checked(div_yield, n_u, "div_yield");
            for (py::ssize_t i = 0; i < n_u; ++i) und[i] = {spot.data()[i], vv[i], qq[i]};
            if (correlation.size() != n_u * n_u) {
                throw std::invalid_argument("correlation must be n_underlyings x n_underlyings");
            }
            std::vector<double> corr(correlation.data(), correlation.data() + correlation.size());
            if (trade_kind.size() != n_t || strike.size() != n_t || maturity.size() != n_t ||
                quantity.size() != n_t) {
                throw std::invalid_argument("trade arrays must all have the same length");
            }
            std::vector<mc::ExposureTrade> trades(n_t);
            for (py::ssize_t i = 0; i < n_t; ++i) {
                const int kind = trade_kind.data()[i];
                if (kind < 0 || kind > 2) throw std::invalid_argument("trade_kind must be 0, 1 or 2");
                trades[i] = {trade_underlying.data()[i], static_cast<mc::TradeKind>(kind),
                             strike.data()[i], maturity.data()[i], quantity.data()[i]};
            }
            std::vector<double> g(grid.data(), grid.data() + grid.size());

            mc::ExposureBlock block;
            {
                py::gil_scoped_release release;
                block = mc::simulate_exposure(und, corr, trades, g, rate, seed, start, count,
                                              tail_k, threads);
            }
            const auto steps = static_cast<py::ssize_t>(g.size());
            return py::dict("sum"_a = to_numpy(std::move(block.sum), {steps}),
                            "sum_sq"_a = to_numpy(std::move(block.sum_sq), {steps}),
                            "tails"_a = to_numpy(std::move(block.tails), {steps, block.tail_k}),
                            "n"_a = block.n);
        },
        "spot"_a, "vol"_a, "div_yield"_a, "correlation"_a, "trade_underlying"_a, "trade_kind"_a,
        "strike"_a, "maturity"_a, "quantity"_a, "grid"_a, "rate"_a, "seed"_a, "start"_a,
        "count"_a, "tail_k"_a, "threads"_a = 1);
}
