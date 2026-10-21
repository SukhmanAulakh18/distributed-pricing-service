# Linux toolchain for the memory and thread checks: Valgrind, plus GCC's
# sanitizer runtimes (Apple clang's ASan runtime does not start on every macOS
# release). CI runs the same script on ubuntu-latest.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ cmake ninja-build valgrind libgtest-dev python3 python3-dev python3-venv util-linux \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir \
        pybind11==3.1.0 scikit-build-core==0.12.2 numpy==2.3.4
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /src
