#!/usr/bin/env bash
# Runs the C++ test suite under AddressSanitizer + UBSan, ThreadSanitizer and
# Valgrind memcheck. Meant for Linux: `make cpp-checks` runs it in the tools
# image, CI runs it on ubuntu-latest.
#
# Usage: scripts/cpp_checks.sh [asan|tsan|valgrind|python-valgrind|all]
set -euo pipefail
cd "$(dirname "$0")/.."
MODE=${1:-all}
OUT=${BUILD_ROOT:-/tmp/build}
# Statistical tests compare against standard errors, so 10x fewer paths keep
# them valid while Valgrind's ~30x slowdown stays tolerable.
export MC_TEST_FAST=1

build() {  # build <dir> <cmake args...>
  cmake -S core -B "$OUT/$1" -G Ninja -DMC_BUILD_TESTS=ON -DMC_BUILD_PYTHON=OFF "${@:2}" >/dev/null
  cmake --build "$OUT/$1" >/dev/null
}

if [[ $MODE == asan || $MODE == all ]]; then
  echo "== AddressSanitizer + UndefinedBehaviorSanitizer"
  build asan -DCMAKE_BUILD_TYPE=RelWithDebInfo -DMC_SANITIZE=address,undefined
  ASAN_OPTIONS=detect_leaks=1:abort_on_error=1 "$OUT/asan/mc_tests" --gtest_brief=1
fi

if [[ $MODE == tsan || $MODE == all ]]; then
  echo "== ThreadSanitizer"
  build tsan -DCMAKE_BUILD_TYPE=RelWithDebInfo -DMC_SANITIZE=thread
  # TSan disables ASLR via personality(), which Docker's default seccomp
  # profile blocks: run this check with --security-opt seccomp=unconfined.
  TSAN_OPTIONS=halt_on_error=1 "$OUT/tsan/mc_tests" --gtest_brief=1
fi

if [[ $MODE == valgrind || $MODE == all ]]; then
  echo "== Valgrind memcheck"
  build valgrind -DCMAKE_BUILD_TYPE=RelWithDebInfo
  valgrind --error-exitcode=1 --leak-check=full --show-leak-kinds=definite,indirect \
    --errors-for-leak-kinds=definite,indirect "$OUT/valgrind/mc_tests" --gtest_brief=1
fi

if [[ $MODE == python-valgrind || $MODE == all ]]; then
  echo "== Valgrind memcheck on the Python extension"
  pip install -q --no-build-isolation --target "$OUT/pyext" . >/dev/null
  for iters in 2 20; do
    # PYTHONMALLOC=malloc routes CPython's allocator through malloc, so
    # Valgrind sees every allocation, including buffers handed to NumPy.
    ITERS=$iters PYTHONPATH="$OUT/pyext" PYTHONMALLOC=malloc valgrind --leak-check=full \
      --show-leak-kinds=definite --num-callers=60 python3 scripts/valgrind_bindings.py > "$OUT/vg_$iters.txt" 2>&1
  done
  if grep -E "Invalid (read|write)|uninitialised|Mismatched free" "$OUT"/vg_*.txt; then exit 1; fi
  # CPython never frees some objects it creates at import, so "definitely
  # lost" is not zero even for a bare `import numpy`. Count only the records
  # whose allocation stack passes through our module; a leak in the bindings
  # would grow with the number of calls, so 2 and 20 rounds must match.
  ours() {
    awk '/definitely lost in loss record/ {n = $2; gsub(",", "", n); rec = 1; mine = 0; next}
         rec && /pricer_core/ {mine = 1}
         rec && /^==[0-9]+== *$/ {if (mine) total += n; rec = 0}
         END {print total + 0}' "$1"
  }
  two=$(ours "$OUT/vg_2.txt"); twenty=$(ours "$OUT/vg_20.txt")
  echo "lost via our module: $two bytes after 2 rounds, $twenty bytes after 20 rounds"
  echo "CPython at exit (not ours): $(grep -oE "definitely lost: [0-9,]+ bytes" "$OUT/vg_20.txt")"
  [[ "$two" == "$twenty" ]] || { echo "FAIL: memory lost via the bindings grows with calls"; exit 1; }
  echo "no invalid accesses; nothing lost per call"
fi
