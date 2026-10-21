"""Exercises every binding (GIL release, zero-copy NumPy in, capsule-owned
NumPy out, C++ exceptions crossing into Python) so Valgrind can check the
extension's memory handling from the Python side."""
import gc
import os
import threading

import numpy as np
import pricer_core as p

for _ in range(int(os.getenv("ITERS", "3"))):
    p.price_european_mc(100, 105, 0.03, 0.2, 1.0, n_paths=20_000, threads=2)
    p.price_asian_mc(100, 100, 0.03, 0.2, 1.0, n_fixings=12, n_paths=20_000, threads=2)
    n = 1000
    out = p.bs_batch(np.full(n, 100.0), np.linspace(80, 120, n), 0.02, np.zeros(n), np.full(n, 0.2),
                     np.ones(n), np.ones(n, bool), threads=2)
    exp = p.simulate_exposure(np.array([100.0, 50.0]), np.array([0.2, 0.3]), np.zeros(2),
                              np.array([[1.0, 0.5], [0.5, 1.0]]), np.array([0, 1], np.int32),
                              np.array([0, 2], np.int32), np.array([100.0, 52.0]), np.array([1.0, 2.0]),
                              np.array([1.0, -3.0]), np.linspace(0.25, 2.0, 8), 0.02, 7, 0, 2_000, 50, 2)
    tails = exp["tails"]
    del out, exp
    gc.collect()
    assert tails.shape == (8, 50)
    del tails
    try:
        p.price_european_mc(100, 105, 0.03, -0.2, 1.0)
    except ValueError:
        pass

threads = [threading.Thread(target=p.price_european_mc, args=(100, 100, 0.02, 0.25, 1.0),
                            kwargs={"n_paths": 10_000}) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
gc.collect()
print("bindings exercised")
