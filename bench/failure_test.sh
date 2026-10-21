#!/usr/bin/env bash
# Kill pods of one component mid-run under steady /price load and record the
# error rate, the window during which clients saw errors, and how long until
# the component is fully Ready again.
#
# Requires the kind cluster from deploy/kind-config.yaml, which maps the
# pricer-api NodePort to localhost:8080.
#
# MODE=crash (default) SIGKILLs the container's main process on the node, like
# an OOM kill or segfault: no SIGTERM, no graceful drain, in-flight requests
# die. MODE=delete force-deletes the pod object instead, which still sends
# SIGTERM to the process and so is closer to a graceful shutdown.
#
# Usage: [MODE=crash|delete] bench/failure_test.sh <app-label> <pods_to_kill> [duration_s] [kill_at_s]
#   bench/failure_test.sh pricer-api 1
#   bench/failure_test.sh pricer-api 2
#   bench/failure_test.sh redis 1
#   bench/failure_test.sh cassandra 1
set -euo pipefail
cd "$(dirname "$0")/.."

APP=${1:?app label, e.g. pricer-api}
KILL=${2:?number of pods to kill}
DURATION=${3:-60}
KILL_AT=${4:-20}
NS=pricing
MODE=${MODE:-crash}
NODE=${NODE:-pricing-control-plane}
OUT=bench/results
PY=${PYTHON:-.venv/bin/python}
mkdir -p "$OUT"

KIND=deploy
case "$APP" in cassandra|redis) KIND=statefulset ;; esac

echo "$APP replicas ready before: $(kubectl -n $NS get $KIND "$APP" -o jsonpath='{.status.readyReplicas}')"

$PY bench/load.py steady --url http://localhost:8080 --duration "$DURATION" \
  --kill-at "$KILL_AT" --out "$OUT/$APP-kill-$KILL.json" &
LOAD=$!

sleep "$KILL_AT"
PODS=$(kubectl -n $NS get pods -l app="$APP" -o name | head -n "$KILL")
echo "[$MODE] at t=${KILL_AT}s: $(echo $PODS | tr '\n' ' ')"
if [ "$MODE" = crash ]; then
  for pod in $PODS; do
    CID=$(docker exec "$NODE" crictl ps -q --label io.kubernetes.pod.name="${pod#pod/}" | head -1)
    PID=$(docker exec "$NODE" crictl inspect -o go-template --template '{{.info.pid}}' "$CID")
    docker exec "$NODE" kill -9 "$PID"
  done
else
  kubectl -n $NS delete $PODS --grace-period=0 --force >/dev/null 2>&1
fi

T0=$(date +%s)
sleep 2
until [ "$(kubectl -n $NS get $KIND "$APP" -o jsonpath='{.status.readyReplicas}')" = \
        "$(kubectl -n $NS get $KIND "$APP" -o jsonpath='{.spec.replicas}')" ]; do
  sleep 1
done
echo "All $APP replicas Ready again after $(( $(date +%s) - T0 ))s"

wait $LOAD
