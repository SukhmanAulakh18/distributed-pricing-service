import gc
import threading

import numpy as np
import pricer_core
import pytest

from conftest import black_scholes


def test_bs_batch_matches_scalar_and_flags_bad_rows():
    rng = np.random.default_rng(1)
    n = 5_000
    s, k = rng.uniform(50, 150, n), rng.uniform(50, 150, n)
    v, t, q = rng.uniform(0.05, 1.0, n), rng.uniform(0.05, 5, n), rng.uniform(0, 0.05, n)
    c = rng.random(n) < 0.5
    v[7] = -1.0  # one bad row
    out = pricer_core.bs_batch(s, k, 0.03, q, v, t, c, threads=4)
    assert np.isnan(out["price"][7]) and np.isnan(out["delta"][7])
    for i in (0, 1, 2, 999, 4999):
        assert out["price"][i] == pytest.approx(black_scholes(s[i], k[i], 0.03, v[i], t[i], c[i], q[i]), abs=1e-10)


def test_bs_batch_rejects_mismatched_shapes():
    with pytest.raises(ValueError, match="same length"):
        pricer_core.bs_batch(np.ones(3), np.ones(2), 0.0, np.zeros(3), np.ones(3), np.ones(3), np.ones(3, bool))


def test_capsule_owned_arrays_outlive_the_call():
    out = pricer_core.simulate_exposure(
        np.array([100.0]), np.array([0.2]), np.array([0.0]), np.eye(1), np.array([0], np.int32),
        np.array([0], np.int32), np.array([100.0]), np.array([1.0]), np.array([1.0]),
        np.array([0.5, 1.0]), 0.01, 3, 0, 2_000, 50, 2)
    tails = out["tails"]
    assert tails.shape == (2, 50) and not tails.flags["OWNDATA"]  # a view onto C++ memory
    del out
    gc.collect()
    assert np.all(np.diff(tails[0]) <= 0)  # still valid and sorted descending
    tails[0, 0] = 1.0  # writable


def test_cpp_errors_become_value_errors():
    with pytest.raises(ValueError, match="positive"):
        pricer_core.price_european_mc(100, 100, 0.01, -0.2, 1.0)
    with pytest.raises(ValueError, match="n_fixings"):
        pricer_core.price_asian_mc(100, 100, 0.01, 0.2, 1.0, n_fixings=0)


def test_results_identical_across_threads_and_concurrent_callers():
    kw = dict(n_paths=200_000, seed=9)
    serial = pricer_core.price_european_mc(100, 100, 0.02, 0.25, 1.0, threads=1, **kw).price.value
    assert pricer_core.price_european_mc(100, 100, 0.02, 0.25, 1.0, threads=8, **kw).price.value == serial
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        pricer_core.price_european_mc(100, 100, 0.02, 0.25, 1.0, threads=2, **kw).price.value)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [serial] * 6
