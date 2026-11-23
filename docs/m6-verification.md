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

## Results, 2026-11-23 (first run)

| Promise | Verdict | Numbers |
|---|---|---|
| Independent deploy | **PASS** (run 2) | payment `ef9c9b8 → 43542ad` through release-payment + promote; 0 other workloads changed, 0 pods replaced, 0 restarts; journey 100% of 453 orders final ≤ 60 s; catalog RPCs 100% of 4530, order 100% of 452 (+1359 messages), inventory / notification messages 100%; users: browse 100%, orders 99.81% (1 of 524 timed out at 12:19:37, before the release — the warm-up stall on the winpc → lab path); no new page |
| Independent deploy | FAIL (run 1, kept) | same isolation (0 changed / replaced / restarted), but journey 73% ≤ 60 s: RabbitMQ's memory alarm (watermark 160 MiB < its idle size) blocked publishers ~40× in 90 min. Fixed in gitops-manifests #132 (watermark 300 MiB) and #133 (the restart that followed crash-looped on the Erlang cookie's permissions — a latent defect) |
| Fault isolation | **not run** | the fault injection (Chaos Mesh pod-failure on inventory) was refused by this session's permission policy; tooling ready (`run-experiment.sh inventory-down --preflight` passes) |
| Eventual consistency | **not run** | same (payment-down) |
| Independent scaling | **not demonstrated** (lab; re-run on winpc below: **FAIL**, 1.0×) | 1 replica: holds 10/s, 20/s once (failed its confirm run), breaks 25/s; order + inventory ×3: holds 20/s twice, breaks 25/s. `check.py scaling` prints 2.00× only because the baseline's 20/s was unstable; both break at 25/s on "saga keeps up" with the node at ~99% of its 8 threads and the single Postgres at 1.8 cores (5 databases, ~10 transactions per order). On this node the limit is the shared CPU and the database, not order / inventory |
| No oversell (flash sale) | **PASS** | 1201 orders accepted for 200 units of M6-SCARCE (30/s × 40 s): exactly 200 committed / 200 CONFIRMED, 1001 CANCELLED, books balance, 0 lost, 0 double charges |

Also found: Prometheus 3 writes integer histogram bucket edges as `le="60.0"` (fixed here, in flash_app and in the
consistency SLO); `winpc.sh stop` could leave a k6 running (fixed).

## Independent scaling on winpc, 2026-11-23 (second attempt)

The lab could not answer the scaling question (its limit was the shared node CPU and the one Postgres for five
databases), so the system was moved to a performance environment of its own: [`perf/winpc/`](../perf/winpc).

**Verdict: FAIL on durable Postgres — scaling only order + inventory raises the knee 1.0× (45/s → 45/s), not ≥ 2×.**
The limit is not order or inventory but a commit-time lock in Postgres that every outbox write takes (below). With
that lock's cost removed (`synchronous_commit=off`, a diagnostic run, not a pass) the same scaling gives **2.0×**
(200/s → 400/s) until the two databases run out of CPU — so the services do scale; the outbox's NOTIFY does not.

### Setup

| | |
|---|---|
| System under test | winpc (i5-11400F, 6C/12T), WSL distro `ci` raised to 12 vCPU / 20 GB (`.wslconfig`), Docker 29.1 + Compose 2.40, project `sf2perf` in `/opt/sf2perf` |
| Images | the v2-lab tags: catalog `3182342`, inventory / notification / order / topology `7f296d2`, payment `43542ad` (runtime v0.5.1); RabbitMQ 3.13.7 (watermark 1 GiB), Postgres 16 ×3 |
| Databases | **pg-order** (orders), **pg-inventory** (inventory), pg-rest (catalog, payment, notification); durable defaults (fsync, `synchronous_commit=on`) |
| CPU limits (per replica) | order 0.5, inventory 0.5, payment 1, notification 0.5, catalog 0.25, pg-order 2, pg-inventory 2, pg-rest 1.5, RabbitMQ 2.5, HAProxy 1 ([`perf.env`](../perf/winpc/perf.env)). "Scaled" = `--scale order=3 --scale inventory=3`: 3× the CPU for exactly those two, all else fixed |
| Entry | HAProxy round-robin over every replica (DNS of the compose network); outbox relay / timeout scanner: one leader per database (advisory lock) — verified, replicas compete for queues |
| Observation | Prometheus (5 s) scraping every replica, RabbitMQ, HAProxy and a cgroup v2 exporter (CPU per container + host; cAdvisor 0.49–0.53 cannot follow Docker 29's containerd image store) |
| Load | k6 v2.4.0 binary on **xiaoxin** (plain process, the lab's k8s untouched), `flash.js`: open-loop PlaceOrder of 1 unit of M6-FLASH-1..3 (stock 3 000 000 each). **Path: Tailscale, not LAN** — xiaoxin and winpc are at different sites (192.168.3.0/24 vs 192.168.11.0/24, different public IPs; direct WireGuard path, ~77 ms RTT). No Kubernetes NodePort on the path (the 11-23 failure mode); steps judged on server-side metrics |
| Judge | [`knee.py`](../perf/winpc/knee.py): 30 s warm-up + 60 s measured per step; holds when server errors < 0.5%, PlaceOrder ≤ 0.5 s ≥ 99%, orders final ≤ 10 s ≥ 99%, orders final / placed ≥ 97%, k6 not-accepted < 0.5% and dropped < 1%. Ascend until a step fails, then repeat the failing rate and the knee (stepping down if it fails); **knee = highest rate that held twice**. The system drains between steps |

### Results

| Run | Postgres | order / inventory replicas | Knee | Above the knee | Ratio |
|---|---|---|---|---|---|
| durable-base | durable | 1 / 1 | **45/s** (held ×2; 50/s held once, failed once on throughput 96.97%) | 60/s fails ×2 (45 final/s) | |
| durable-x3 | durable | 3 / 3 | **45/s** (held ×2) | 50/s fails ×2 (placement ≤ 0.5 s only 45% / 29%) | **1.0×** |
| async-base (diagnostic) | `synchronous_commit=off` | 1 / 1 | **200/s** (held ×2) | 240/s fails ×2 (~209 final/s) | |
| async-x3 (diagnostic) | `synchronous_commit=off` | 3 / 3 | **400/s** (held ×2) | 480/s fails ×2 (286 / 393 final/s) | **2.0×** |

CPU (cores) at the knee and the first failing step, from the cgroups (limits in brackets):

| Step | order | inventory | pg-order | pg-inventory | RabbitMQ | host busy | What ran out |
|---|---|---|---|---|---|---|---|
| durable-base 45/s | 0.20 [0.5] | 0.15 [0.5] | 0.25 [2] | 0.18 [2] | 0.38 [2.5] | 1.9 / 12 | — |
| durable-base 60/s ✗ | 0.22 | 0.16 | 0.26 | 0.20 | 0.39 | 2.0 / 12 | nothing: the commit lock |
| durable-x3 50/s ✗ | 0.26 [1.5] | 0.19 [1.5] | 0.27 | 0.21 | 0.41 | 2.2 / 12 | nothing: the commit lock |
| async-base 200/s | 0.50 [0.5] | 0.43 | 0.87 | 0.68 | 0.67 | 4.3 / 12 | — |
| async-base 240/s ✗ | **0.50** [0.5] | 0.46 | 0.95 | 0.76 | 0.70 | 4.6 / 12 | order's CPU |
| async-x3 400/s | 1.34 [1.5] | 0.92 [1.5] | 1.83 | 1.30 | 1.20 | 8.2 / 12 | — |
| async-x3 480/s ✗ | 1.28–1.35 | 0.74–0.98 | **1.98–1.99** [2] | **1.70–2.00** [2] | 1.17–1.28 | 8.7–8.9 / 12 | the databases' CPU |

Every step, every run: 0 server errors, 0 DLQ, PlaceOrder accepted 100% (k6). Full per-step tables:
`perf/winpc/report.py perf/winpc/runs/2026-11-23/*.json`.

### Why the durable knee does not move

- At 60/s (1 replica) the saga finishes only ~45 orders/s, and at 50/s with 3 replicas still ~48/s, while **nothing
  is near its CPU limit** (order 0.22 of 0.5, host 2 of 12 cores) and Postgres connections are mostly idle.
- Sampling `pg_stat_activity` on pg-order under 80/s: most active backends wait on **`Lock / object`** with query
  `commit`, `pg_locks` shows `AccessExclusiveLock` on object `pg_database` 0 — the global lock a transaction that
  called `NOTIFY` takes at commit (`PreCommit_Notify`), held until its commit record is flushed — and one backend in
  `WALSync`.
- api-factory-runtime's `outbox.Writer.Write` does `SELECT pg_notify('factory_outbox', '')` in every transaction that
  writes the outbox (outbox.go: wakes the relay). In the order database that is PlaceOrder and each saga step
  (reserved → charge, succeeded → commit, committed → confirmed); in inventory, reserve and commit. So those commits
  run **one at a time per Postgres instance**, each paying a WAL flush (`pg_test_fsync` on the pg-order volume:
  fdatasync ≈ 1.8 ms, ~550/s). That ceiling belongs to the database, not to a replica: more order replicas only
  queue more transactions at the same lock — placement latency got *worse* (p95 0.49 s → 2.2 s at 50/s).
- The diagnostic confirms it: with `synchronous_commit=off` the commit no longer waits for the flush while holding
  the lock and the knee goes 45 → 200/s with the same single replicas; the limit becomes order's own CPU (0.50 of
  0.5), and scaling order + inventory then doubles it (400/s) until pg-order / pg-inventory reach their 2 cores.

This is also what capped the lab on 11-23 (one CNPG for five databases: same lock, plus the shared CPU).

### What would be needed for ≥ 2× on durable storage

1. **Take NOTIFY out of the write transaction** (api-factory-runtime, outbox): wake the relay another way (poll at a
   short interval — it already polls every 1 s as the fallback — or NOTIFY only when the relay reports idle, or
   notify after commit outside the transaction). This removes the cluster-wide serialization; the commits then group
   (`commit_delay` / group commit) instead of queueing. Expected from the diagnostic: the knee becomes CPU-bound and
   scales with replicas, as in the async runs. Then re-run this test.
2. Until then, the database's flush rate sets the flash-sale knee: faster WAL storage, or `synchronous_commit=off`
   for the order / inventory databases (accepting the loss of the last ~600 ms of commits on a crash — not
   acceptable for orders without a decision).
3. For 2× beyond ~400/s on this host: more CPU for pg-order and pg-inventory (they were the next limit) — that is
   "scale the databases too", outside the promise as worded.

### Reproduce

```sh
cd perf/winpc
./perf.sh sync && ./perf.sh firewall on          # files to winpc / xiaoxin; admit xiaoxin's tailnet IP to the perf ports
./perf.sh up && ./perf.sh seed                   # baseline: one replica each, durable
./perf.sh knee durable-base --rates 10,15,20,25,30,35,40,45,50,60,70,80,100,120   # ≈ 30 min; ./perf.sh log durable-base
./perf.sh up order=3 inventory=3 && ./perf.sh seed
./perf.sh knee durable-x3 --rates 30,40,45,50,60,70,80,100,120,140
# diagnostic: ./perf.sh up [order=3 inventory=3] sync=off …
./perf.sh fetch && ./report.py results/results/*.json
./perf.sh down && ./perf.sh firewall off         # leave winpc idle (volumes stay)
```

## Known limits

- The outbox relay is one leader per database, and order's saga timeout scanner adds a round every few seconds:
  scaling order adds PlaceOrder handlers and saga consumers, not relays. Inventory's reserves on one SKU serialize on
  its row lock whatever the replica count. If the knee does not move, the report's CPU / neighbour lines say where the
  limit is (Postgres, the relay, the node).
- capacity.py waits 60 s between a breaking step and its refine steps; a saga backlog from the breaking step can spill
  into the next one (the `confirm` repetitions catch disagreements: "unstable").
- `winpc.sh` keeps one run at a time (task `M6K6`); the capacity runner uses its own task (`LoadTest`).
