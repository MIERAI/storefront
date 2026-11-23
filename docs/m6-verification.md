# M6: verifying the four microservice promises

The v2 design ([§0](design/v2-microservices.md#0-目标和成功标准)) promises four things and says each must be
*measured*, not claimed. This runbook is how M6 measures them on the lab (xiaoxin, namespace `v2-lab`). The tools are
in [`scripts/m6/`](../scripts/m6); results land in `scripts/m6/out/` (ignored by Git; copy the tables you keep into
the M6 report).

| Promise | Pass means | Experiment | Judged by |
|---|---|---|---|
| **Independent deploy** | releasing payment changes and restarts nothing else; the journey SLO and every other service's SLIs hold; no page | a real release of payment (a commit under `services/payment` → `release-payment` → promote commit → Argo CD) under light load | `snapshot.py` before / after, `check.py deploy` |
| **Fault isolation** | inventory down 10 min: browsing ≥ 99.5% (users, inside the outage); orders still accepted; none lost; all final after recovery | `run-experiment.sh inventory-down` (Chaos Mesh pod-failure on every inventory pod) | `watch.py` timeline, `check.py isolation` |
| **Eventual consistency** | payment down 10 min: every order created before the recovery final ≤ 5 min after it; no double charge; no charge kept for a cancelled order; no oversell | `run-experiment.sh payment-down` | `watch.py` timeline, `check.py consistency` |
| **Independent scaling** | flash sale (3 hot SKUs): scaling only order + inventory raises the knee ≥ 2× | slo-platform `capacity.py` from winpc, at 1 replica, then order + inventory scaled | `check.py scaling` (+ `check.py stock` for the oversell rush) |

Plus the consistency SLOs in gitops-manifests `v2/app.yaml`: `consistency-final-5m` (orders final within 5 minutes,
`factory_sm_completion_seconds`) and `consistency-none-stuck` (no order waiting ≥ 5 min in one state, recording rule
`storefront:order_saga_none_stuck_5m:bool`).

## Safety

- Every tool calls `m6lib.guard()` first: the kubeconfig must be an explicit file (`M6_KUBECONFIG`, default
  `~/.kube/xiaoxin.yaml`; `~/.kube/config` and anything named `laptop` are refused), its current context must be
  `xiaoxin`, every node must carry `slo-platform/lab=true`, and the namespace must look like v2's (`*v2*`), not be
  another application's, exist and run catalog / order / inventory / payment. **Faults are never injected on the
  laptop** — it fails two of these checks.
- Checks are read only: SQL in `default_transaction_read_only` sessions on the CNPG primary (`kubectl exec … psql`
  over the local socket: no credentials leave the pod), Prometheus through the API server's service proxy.
- Faults end by themselves: Chaos Mesh `spec.duration: 10m`, removed earlier by `experiment.py`; the namespace is
  marked while an experiment runs (one at a time).
- Writes the tools make: `expose.py up` (two kinds of objects labelled `storefront.m6/expose=true`, removed by
  `expose.py down`), the PodChaos objects (experiment.py), scaling through Git (below).

## How the load reaches the lab

k6 runs on **winpc** (`F:\k6\k6.exe` v2.4.0), outside the cluster, so the load does not compete for the CPU it
measures; winpc reaches the lab node at its Tailscale address `100.76.236.114` (≈ 45 ms RTT, direct). In-cluster k6
would need a Docker Hub image — avoided.

- `expose.py up` creates a NodePort Service `<svc>-m6` for catalog, order and inventory (same selector as the stack's
  Service) and a NetworkPolicy `m6-loadgen` that admits their `http` port from anywhere: the stack's own policies
  admit it only from `v2-lab` and `monitoring`. Neither object is in Git (Argo CD leaves them alone). It writes
  `out/urls.env`, which `winpc.sh` passes to every k6 run. The capacity runner's own `order-capacity` NodePort goes
  through the same NetworkPolicy, so keep the exposure up during the capacity test too.
- `winpc.sh start <script>` runs k6 as the scheduled task `M6K6` (survives ssh drops and a sleeping Mac), `fg` in the
  foreground; `status`, `wait`, `fetch`, `stop`. Long runs write `--out csv=<run>.csv.gz`, which lets `check.py` compute
  the browse ratio *inside* the outage window.

k6 scripts (`scripts/m6/k6/`, Connect JSON over HTTP):

| Script | What |
|---|---|
| `seed.js` | `CreateProduct` for `M6-ITEM-01..20`, `M6-FLASH-1..3`, `M6-SCARCE`, `M6-PRICY` (catalog → `catalog.product_created` → inventory `InitStock`), then `AdjustStock` up to 100000 each (not M6-SCARCE). Idempotent |
| `load.js` | browse (`ListProducts` category m6 / `GetProduct`) at `BROWSE_RPS` + `PlaceOrder` at `ORDER_RPS` for `DURATION`; customer ids `TAG×10⁶ + n` so the checks find exactly this run's orders; 2% of orders contain `M6-PRICY` (above payment's 100000 limit → compensation path) |
| `flash-oversell.js` | sets M6-SCARCE to exactly `SCARCE_STOCK` (200), then 50 orders/s × 30 s of 1 unit each |

## The run, in order

Prerequisites: the stack healthy (`kubectl -n v2-lab get pods`), `SLO_PLATFORM` = a checkout of slo-platform with
`scripts/experiment.py` and `scripts/capacity.py` (today: `~/workspace/gitops/slo-platform`, branch `feat/capacity`),
`uv`, ssh to winpc. From the storefront checkout:

```sh
cd scripts/m6
export M6_NAMESPACE=v2-lab M6_KUBECONFIG=~/.kube/xiaoxin.yaml
./expose.py up                                   # NodePorts + m6-loadgen NetworkPolicy → out/urls.env   (≈1 min)
./winpc.sh fg seed.js                            # products + stock                                    (≈1 min)
./run-experiment.sh inventory-down --preflight   # guard, Chaos Mesh annotation, no running experiment
```

### 1. Independent deploy (≈ 25 min)

```sh
TAG=$(date +%m%d%H%M)
./winpc.sh start load.js -e TAG=$TAG -e BROWSE_RPS=10 -e ORDER_RPS=1 -e DURATION=30m
./snapshot.py --out out/deploy-before.json
# release payment the normal way: a one-line change under services/payment (e.g. CHANGELOG.md), PR, merge →
# release-payment (test, signed image, promote commit to gitops-manifests v2/overlays/lab) → Argo CD syncs v2-lab
./snapshot.py --wait-rollout payment --baseline out/deploy-before.json --soak 5m --out out/deploy-after.json
./winpc.sh stop; ./winpc.sh fetch
./check.py deploy --before out/deploy-before.json --after out/deploy-after.json --target payment \
  --k6-summary out/k6/<run>.summary.json --out out/deploy
```

The `topology` Job is an Argo CD Sync hook and is re-created by every sync of `v2-lab` by design: its pods are left
out of the comparison.

### 2. Fault isolation (≈ 20 min) and 3. eventual consistency (≈ 20 min)

```sh
./run-experiment.sh inventory-down     # → out/inventory-down-<TAG>.md / .json, experiment.py's report in out/
./run-experiment.sh payment-down       # → out/payment-down-<TAG>.md / .json
```

Each: k6 load (browse 20/s, orders 2/s) for 2 min baseline + 10 min fault + 6 min after; `watch.py` records when
the target lost its last ready pod, when it got one back (= the recovery) and polls the run's orders by status every
15 s until every order created before the recovery is final; `experiment.py` injects the fault and checks the alerts
(`v2-browse-availability` and `v2-place-order-availability` must not page; other `v2-lab` pages are allowed:
the consistency SLOs are expected to burn); then `check.py`.

`check.py` rows shared by both: no accepted order lost (orders in `orders` ≥ k6's accepted count), every order final
now, no double charge (≤ 1 payment per order, each CONFIRMED order exactly one succeeded payment = its total), no
charge kept for a cancelled order, stock follows the orders (CONFIRMED: every line committed; CANCELLED: nothing
committed or held), no oversell (every M6 SKU: available ≥ 0, reserved = held reservations, available + reserved +
committed = everything ever received), outboxes drained, nothing new dead-lettered. Plus, not judged: saga resends,
duplicates dropped, late answers ignored.

### 4. Independent scaling (≈ 60–80 min)

```sh
./flash_app.py                                         # → out/flash-sale.app.yaml (namespace v2-lab = app v2, env lab)
./winpc.sh fg seed.js                                  # top the flash SKUs up again
~/workspace/gitops/bin/loadtest.sh start out/flash-sale.app.yaml lab     # baseline: order, inventory at 1 replica
~/workspace/gitops/bin/loadtest.sh status ; ~/workspace/gitops/bin/loadtest.sh fetch out/capacity
# scale order + inventory through Git (gitops-manifests v2/base/services.yaml replicas, a PR), wait for the pods
~/workspace/gitops/bin/loadtest.sh start out/flash-sale.app.yaml lab     # scaled
./check.py scaling --baseline out/capacity/<…>-v2-lab-capacity.json --scaled out/capacity/<…>.json --out out/scaling
# then set the replicas back to 1 (PR)
TAG=$(date +%m%d%H%M); ./winpc.sh fg flash-oversell.js -e TAG=$TAG    # oversell rush (≈1 min)
./check.py stock --tag $TAG --k6-summary out/k6/<run>.summary.json --out out/oversell
```

A step holds when PlaceOrder has no server errors (99.5%), answers within 0.5 s (99%) and **the saga keeps up**:
99% of orders final within 10 s (`factory_sm_completion_seconds`). The last one makes it a flash-sale test: placing an
order only writes order's database; the contention is inventory's conditional UPDATE on three rows. Before scaling,
check the node's memory requests (`kubectl describe node`): above 85%, scale to 2 instead of 3 and say so.

Finally `./expose.py down`.

## Reading the results

- **PASS** = every judged row passed; **FAIL** = one failed; **INCONCLUSIVE** = something could not be measured (N/A
  rows: no samples, Prometheus without data). INFO rows are not judged.
- A miss is a result: record the numbers and the cause, do not re-run until it passes.
- The capacity numbers hold for *this* lab node (4 cores, 7 GB, Argo CD / Prometheus / k3s as neighbours; see the
  report's neighbour CPU). The 2× promise is about the ratio, measured on the same node within the hour.

## Known limits

- The outbox relay is one leader per database, and order's saga timeout scanner adds a round every few seconds:
  scaling order adds PlaceOrder handlers and saga consumers, not relays. Inventory's reserves on one SKU serialize on
  its row lock whatever the replica count. If the knee does not move, the report's CPU / neighbour lines say where the
  limit is (Postgres, the relay, the node).
- capacity.py waits 60 s between a breaking step and its refine steps; a saga backlog from the breaking step can spill
  into the next one (the `confirm` repetitions catch disagreements: "unstable").
- `winpc.sh` keeps one run at a time (task `M6K6`); the capacity runner uses its own task (`LoadTest`).
