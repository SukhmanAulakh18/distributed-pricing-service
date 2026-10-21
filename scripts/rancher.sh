#!/usr/bin/env bash
# Starts Rancher (single-container install, for local use) on the kind
# network and imports the kind cluster through Rancher's API, so the pricing
# workloads, nodes, events and logs can be viewed and managed from Rancher.
#
# The admin password is generated into deploy/rancher/.bootstrap-password
# (gitignored). The first time you open the UI, Rancher asks you to accept its
# terms; that is left to you.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=deploy/rancher
R=https://localhost:8443
VERSION=${RANCHER_VERSION:-v2.15.2}   # supports Kubernetes 1.34-1.36; kind-config.yaml pins 1.36
mkdir -p "$DIR"

if ! docker inspect rancher >/dev/null 2>&1; then
  [ -s "$DIR/.bootstrap-password" ] || (umask 077; python3 -c "import secrets; print(secrets.token_urlsafe(18))" > "$DIR/.bootstrap-password")
  docker run -d --name rancher --network kind --restart unless-stopped -p 8443:443 --privileged \
    -e CATTLE_BOOTSTRAP_PASSWORD="$(cat "$DIR/.bootstrap-password")" "rancher/rancher:$VERSION" >/dev/null
fi
echo "waiting for Rancher..."
until curl -sk -m 3 "$R/ping" | grep -q pong; do sleep 5; done

TOKEN=$(curl -sk -X POST "$R/v3-public/localProviders/local?action=login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"admin\",\"password\":\"$(cat "$DIR/.bootstrap-password")\",\"responseType\":\"token\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['token'])")
(umask 077; echo "$TOKEN" > "$DIR/.api-token")
api() { curl -sk -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; }

# Agents in the kind nodes reach Rancher by its container name on the kind network.
api -X PUT "$R/v3/settings/server-url" -d '{"name":"server-url","value":"https://rancher"}' >/dev/null

CID=$(api "$R/v3/clusters?name=pricing-kind" | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(d[0]['id'] if d else '')")
if [ -z "$CID" ]; then
  CID=$(api -X POST "$R/v3/clusters" -d '{"type":"cluster","name":"pricing-kind","description":"kind: 1 control plane + 3 workers across 3 zones"}' \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
fi
echo "$CID" > "$DIR/.cluster-id"
until URL=$(api "$R/v3/clusterregistrationtokens?clusterId=$CID" | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(d[0]['manifestUrl'] if d else '')") && [ -n "$URL" ]; do sleep 3; done
# The manifest URL names the in-network host; fetch it through the published port.
curl -sk "${URL/https:\/\/rancher/$R}" | kubectl apply -f - >/dev/null
echo "waiting for the cluster to become active in Rancher..."
until api "$R/v3/clusters/$CID" | grep -q '"state":"active"'; do sleep 5; done
echo "Rancher: $R  (user admin, password in $DIR/.bootstrap-password)"
echo "Cluster: $R/dashboard/c/$CID/explorer"
