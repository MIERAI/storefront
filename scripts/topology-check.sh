#!/usr/bin/env bash
# topology/Dockerfile's RUNTIME_VERSION (the CLI that applies contracts/topology.json) must be the runtime every v2
# service is generated against (services/*/go.mod): the manifest's format belongs to that runtime version.
set -euo pipefail
cd "$(dirname "$0")/.."
want=$(sed -n 's/^ARG RUNTIME_VERSION=//p' topology/Dockerfile)
fail=0
for m in services/*/go.mod; do
  have=$(awk '$1 == "github.com/MIERAI/api-factory-runtime" {print $2}' "$m")
  [[ -z $have ]] && continue
  [[ $have == "$want" ]] || { echo "✗ $m: api-factory-runtime $have, topology/Dockerfile: $want"; fail=1; }
done
(( fail == 0 )) && echo "✓ topology CLI = services' runtime ($want)"
exit $fail
