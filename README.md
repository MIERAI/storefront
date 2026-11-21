# storefront

> Example application of [slo-platform](https://github.com/MIERAI/slo-platform) — deployed by its
> [examples/storefront](https://github.com/MIERAI/slo-platform/tree/main/examples/storefront); principles and shared CI:
> [GitOps Platform Blueprint](https://github.com/MIERAI/gitops-platform-blueprint)

A monorepo with three services, each released on its own (`release-<service>`: only changes under its directory) and
promoted on its own through three environments, dev → staging → prod:

| Service | | Manifests (gitops-manifests) |
|---|---|---|
| `services/web` | the page; takes orders (`POST /orders`) | kustomize `storefront/overlays/<env>` |
| `services/api` | products | kustomize `storefront/overlays/<env>` |
| `services/worker` | processes the orders | Helm chart `storefront-worker/chart` + `values-<env>.yaml` |

The third reference shape of the platform, next to sample-app (one service, push) and api-factory (design PR →
generated code).

## The order pipeline (asynchronous)

```
user ─POST /orders→ web ─INSERT (pending)→ Postgres ←claim (FOR UPDATE SKIP LOCKED), work, done─ worker ×2
                     └─ watches the pipeline (internal/orders Check): backlog age, overdue orders
```

Postgres is a CloudNativePG cluster per environment (gitops-manifests `storefront/base/postgres.yaml`); web and
worker read `DATABASE_URL` from the secret the operator generates and create the table at start (`internal/orders`).
A claimed order stays locked in the worker's transaction until it is done, so a worker that dies rolls it back to
pending instead of losing it. The SLIs of this shape (freshness, completeness) are exported by **web**, not the
worker: a gauge exported by the worker would disappear with the last worker, exactly when it matters.

| Metric | From | SLI |
|---|---|---|
| `http_requests_total{route="POST /orders"}` | web | order intake availability |
| `orders_processed_through_timestamp_seconds` | web (every 10s; absent while the database cannot be asked) | freshness: `time() -` it, the age of the oldest unprocessed order (or ~0) |
| `orders_overdue_total` / `orders_created_total` | web | completeness: orders not processed within 5 min, counted once |
| `orders_pending`, `orders_oldest_pending_age_seconds`, `order_processing_delay_seconds`, `orders_processed_total`, `worker_errors_total` | web / worker | dashboards |

## Run it locally

Build and test (the `internal/orders` tests are skipped without a Postgres; CI runs one):

```sh
go build ./... && go test ./...

docker run -d --rm --name storefront-pg -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:16-alpine
TEST_DATABASE_URL='postgres://postgres:test@localhost:5432/postgres?sslmode=disable' go test ./...
```

One service on its own: `go run ./services/api`, then `curl localhost:8080/products`. Every service listens on
`:8080` (and `:9090` for `/metrics`), so the whole pipeline runs as containers on one network:

```sh
DB='postgres://postgres:test@pg:5432/postgres?sslmode=disable'
docker network create storefront
docker run -d --name pg --network storefront -e POSTGRES_PASSWORD=test postgres:16-alpine
for s in api web worker; do docker build -f services/$s/Dockerfile -t storefront-$s .; done
docker run -d --name api --network storefront storefront-api
docker run -d --name worker --network storefront -e DATABASE_URL=$DB storefront-worker
docker run -d --name web --network storefront -p 8080:8080 -e API_URL=http://api:8080 -e DATABASE_URL=$DB storefront-web

curl localhost:8080/                                   # the page, with the api's products
curl -X POST localhost:8080/orders -d '{"amount":1200}'  # {"id":1}; the worker marks it done within a second
```
