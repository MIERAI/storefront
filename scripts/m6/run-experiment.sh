#!/usr/bin/env bash
# One fault experiment of M6, end to end (docs/m6-verification.md):
#
#   run-experiment.sh inventory-down|payment-down [--preflight]
#
#   1. guard: lab context, lab nodes, the v2 namespace (m6lib.guard); the experiment's namespace must be it
#   2. render experiments/<name>.yaml (namespace, the browse SLOs that must stay quiet) into out/
#   3. start the users on winpc (k6 load.js: browse + orders, TAG = this run) and wait BASELINE seconds
#   4. watch.py in the background (the target's readiness, the run's orders by status)
#   5. slo-platform scripts/experiment.py: Chaos Mesh pod-failure on every pod of the target for 10 minutes, the
#      alert expectations; the fault ends by itself even if this machine dies
#   6. wait for watch.py (every order created before the recovery final, or 20 min) and for k6, fetch the k6 results
#   7. check.py isolation (inventory-down) / consistency (payment-down) → out/<name>-<TAG>.md / .json
#
# --preflight: steps 1–2 plus the checks below, nothing started.
# Environment: M6_NAMESPACE (v2-lab), M6_KUBECONFIG (~/.kube/xiaoxin.yaml), SLO_PLATFORM (a checkout of
# MIERAI/slo-platform main, default ~/workspace/gitops/slo-platform), M6_QUIET_SLOS (comma-separated Pyrra slo labels
# of the SLOs that must not page, default v2-browse-availability,v2-place-order-availability), BROWSE_RPS (20), ORDER_RPS (2),
# BASELINE (120 s), AFTER (360 s of load after the fault).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=${1:?usage: run-experiment.sh inventory-down|payment-down [--preflight]}
MODE=${2:-run}
NS=${M6_NAMESPACE:-v2-lab}
KCFG=${M6_KUBECONFIG:-$HOME/.kube/xiaoxin.yaml}
SLO_PLATFORM=${SLO_PLATFORM:-$HOME/workspace/gitops/slo-platform}
QUIET_SLOS=${M6_QUIET_SLOS:-v2-browse-availability,v2-place-order-availability}
BROWSE_RPS=${BROWSE_RPS:-20} ORDER_RPS=${ORDER_RPS:-2} BASELINE=${BASELINE:-120} AFTER=${AFTER:-360}
case $NAME in
  inventory-down) TARGET=inventory CHECK=isolation ;;
  payment-down)   TARGET=payment   CHECK=consistency ;;
  *) echo "unknown experiment $NAME" >&2; exit 1 ;;
esac
OUT=$HERE/out; mkdir -p "$OUT"
export M6_NAMESPACE=$NS M6_KUBECONFIG=$KCFG
k() { kubectl --kubeconfig "$KCFG" --context xiaoxin "$@"; }

# 1. the guard (raises unless lab + v2 namespace)
python3 -c "import sys; sys.path.insert(0, '$HERE'); import m6lib; m6lib.guard_or_exit('$NS'); print('guard: ok ($NS on xiaoxin)')"

# 2. render the experiment
python3 - "$HERE/experiments/$NAME.yaml" "$OUT/$NAME.yaml" "$NS" "$QUIET_SLOS" <<'PY'
import json, sys
src, dst, ns, quiet = sys.argv[1:]
q = [{"alert": "ErrorBudgetBurn", "labels": {"namespace": ns, "slo": s.strip()}} for s in quiet.split(",") if s.strip()]
text = open(src).read().replace("${NAMESPACE}", ns).replace("${QUIET}", json.dumps(q))
assert "${" not in text, "unfilled placeholder"
open(dst, "w").write(text)
PY
grep -q "^namespace: $NS\$" "$OUT/$NAME.yaml" || { echo "rendered experiment is not for $NS" >&2; exit 1; }
echo "experiment: $OUT/$NAME.yaml (quiet: $QUIET_SLOS)"

# preflight checks: nothing is started by these
fail=0
[ -f "$SLO_PLATFORM/scripts/experiment.py" ] || { echo "✗ no $SLO_PLATFORM/scripts/experiment.py (SLO_PLATFORM)"; fail=1; }
[ -f "$OUT/urls.env" ] || { echo "✗ no $OUT/urls.env (expose.py up)"; fail=1; }
inject=$(k get namespace "$NS" -o 'jsonpath={.metadata.annotations.chaos-mesh\.org/inject}')
mark=$(k get namespace "$NS" -o 'jsonpath={.metadata.annotations.slo-platform/experiment}')
[ "$inject" = enabled ] || { echo "✗ $NS lacks the annotation chaos-mesh.org/inject=enabled (Chaos Mesh would not inject)"; fail=1; }
[ -z "$mark" ] || { echo "✗ $NS is marked by a running experiment: $mark"; fail=1; }
pods=$(k get pods -n "$NS" -l "app=$TARGET" -o name | wc -l | tr -d ' ')
[ "$pods" -ge 1 ] || { echo "✗ no pod with label app=$TARGET in $NS (the PodChaos selector)"; fail=1; }
k get --raw /api/v1/namespaces/monitoring/services/kps-prometheus:9090/proxy/api/v1/query?query=up >/dev/null || { echo "✗ lab Prometheus not reachable"; fail=1; }
[ $fail = 0 ] || exit 1
echo "preflight: ok ($pods $TARGET pods, Chaos Mesh enabled, no running experiment)"
[ "$MODE" = "--preflight" ] && exit 0

# 3. users
TAG=$(date +%m%d%H%M)
mins=$(( (BASELINE + 600 + AFTER + 59) / 60 ))
RUN=$("$HERE/winpc.sh" start load.js -e "TAG=$TAG" -e "BROWSE_RPS=$BROWSE_RPS" -e "ORDER_RPS=$ORDER_RPS" -e "DURATION=${mins}m" | tail -1)
echo "k6 on winpc: $RUN (TAG $TAG, ${mins} min, browse ${BROWSE_RPS}/s, orders ${ORDER_RPS}/s); baseline ${BASELINE}s"
sleep "$BASELINE"

# 4. timeline
TL=$OUT/$NAME-$TAG.timeline.json
python3 "$HERE/watch.py" --namespace "$NS" --target "$TARGET" --tag "$TAG" --out "$TL" > "$OUT/$NAME-$TAG.watch.log" 2>&1 &
WATCH=$!
trap 'kill $WATCH 2>/dev/null || true' EXIT

# 5. the fault (experiment.py removes it at the end; Chaos Mesh ends it after 10 min anyway)
set +e
uv run --quiet --with pyyaml python3 "$SLO_PLATFORM/scripts/experiment.py" --kubeconfig "$KCFG" --context xiaoxin \
  --results "$OUT" "$OUT/$NAME.yaml"
EXP=$?
set -e
echo "experiment.py exit $EXP (0 pass, 1 fail, 2 invalid): report in $OUT"

# 6. wait for the orders to settle, then for k6
wait $WATCH || true
trap - EXIT
tail -3 "$OUT/$NAME-$TAG.watch.log"
"$HERE/winpc.sh" wait
"$HERE/winpc.sh" fetch "$OUT/k6" >/dev/null

# 7. the verdict
set +e
python3 "$HERE/check.py" "$CHECK" --namespace "$NS" --timeline "$TL" --k6-summary "$OUT/k6/$RUN.summary.json" \
  $( [ "$CHECK" = isolation ] && echo --k6-csv "$OUT/k6/$RUN.csv.gz" ) --out "$OUT/$NAME-$TAG"
RC=$?
echo "check.py $CHECK exit $RC (0 PASS, 1 FAIL, 2 INCONCLUSIVE); alerts: experiment.py exit $EXP"
exit $RC
