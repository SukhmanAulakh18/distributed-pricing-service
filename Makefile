PY ?= .venv/bin/python
PIP ?= .venv/bin/pip
COMPOSE = docker compose -f deploy/docker-compose.yml

.PHONY: install test cpp-test cpp-checks bench plots up down cluster deploy rancher demo chaos triage

install:
	$(PIP) install -r requirements-dev.txt
	$(PIP) install --no-build-isolation .

test: cpp-test
	$(PY) -m pytest -q

cpp-test:
	PATH="$(CURDIR)/.venv/bin:$$PATH" cmake -S core -B build/cpp -G Ninja -DMC_BUILD_TESTS=ON -DMC_BUILD_PYTHON=OFF
	PATH="$(CURDIR)/.venv/bin:$$PATH" cmake --build build/cpp
	./build/cpp/mc_tests --gtest_brief=1

# Sanitizers and Valgrind run in a Linux container (see docker/tools.Dockerfile).
cpp-checks:
	docker build -q -f docker/tools.Dockerfile -t mc-tools .
	docker run --rm --security-opt seccomp=unconfined -v "$(CURDIR)":/src:ro mc-tools scripts/cpp_checks.sh all

bench:
	$(PY) bench/bench_core.py

plots:
	$(PY) bench/plots.py

up:
	$(COMPOSE) up --build -d

down:
	$(COMPOSE) down

KEDA_VERSION = v2.20.2

# A 4-node, 3-zone kind cluster with metrics-server and KEDA, then the app and
# its observability stack (deploy/k8s/overlays/kind).
cluster:
	kind create cluster --config deploy/kind-config.yaml
	kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.8.0/components.yaml
	kubectl -n kube-system patch deploy metrics-server --type=json \
	  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
	kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/$(KEDA_VERSION)/keda-$(subst v,,$(KEDA_VERSION)).yaml
	kubectl -n keda rollout status deploy/keda-operator --timeout=300s
	$(MAKE) deploy

deploy:
	docker build -t mc-pricer:dev .
	kind load docker-image mc-pricer:dev --name pricing
	kubectl apply -k deploy/k8s/overlays/kind
	kubectl -n pricing rollout status statefulset/cassandra --timeout=900s
	kubectl -n pricing rollout status deploy/pricer-api --timeout=300s

rancher:
	scripts/rancher.sh

chaos:
	$(PY) bench/chaos.py --minutes 10

demo:
	$(PY) scripts/demo.py

triage:
	$(PY) -m tools.triage --api http://localhost:8080 --namespace pricing

