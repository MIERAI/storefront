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

## Traces (OpenTelemetry, off by default)

Every service traces when an OTLP endpoint is configured, by the standard environment variables only
(`internal/obs` `Tracing`): `OTEL_EXPORTER_OTLP_ENDPOINT` (OTLP over HTTP, e.g. `http://tempo:4318`),
`OTEL_SERVICE_NAME` (default `web` / `api` / `worker`), `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_TRACES_SAMPLER(_ARG)`
(default `parentbased_always_on`). Without an endpoint, or with `OTEL_SDK_DISABLED=true`, tracing is a no-op: no
exporter, no connection, no log. The W3C `traceparent` is propagated either way.

| Span | Kind | From |
|---|---|---|
| `GET /{$}`, `POST /orders`, `GET /products` (the mux pattern, as the metrics' `route`; attribute `result=degraded` on a degraded page) | server | web, api (not `/healthz`) |
| `GET /products` (`peer.service=api`) | client | web → api, `traceparent` injected |
| `INSERT orders`, `UPDATE orders`, `BEGIN`, `COMMIT`, … (`db.query.text`, no parameters) | client | web, worker (`internal/orders`) |
| `process order` (`order.id`, `order.delay_seconds`): the work, the `UPDATE`, the commit | internal | worker, one trace per order |

Queries outside a trace (the 10 s pipeline check, migrations, an empty poll, the claim) are not traced: thousands of
one-span traces a day that answer no question. The metrics are unchanged (no exemplars: the text format 0.0.4 has
none). An unreachable endpoint costs one warning per batch (5 s), never a request. In Kubernetes: slo-platform's
optional `tracing` component (Grafana Tempo) and its storefront example. Locally, next to the containers of
[Run it locally](#run-it-locally):

```sh
cat > tempo.yaml <<'EOF'
server: { http_listen_port: 3200 }
distributor: { receivers: { otlp: { protocols: { http: { endpoint: 0.0.0.0:4318 } } } } }
storage: { trace: { backend: local, local: { path: /var/tempo/traces }, wal: { path: /var/tempo/wal } } }
EOF
docker run -d --name tempo --network storefront -v $PWD/tempo.yaml:/etc/tempo.yaml -p 3200:3200 grafana/tempo:3.1.1 -config.file=/etc/tempo.yaml
# add -e OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4318 to each service's docker run; traces are searchable after ~30 s:
curl -sG localhost:3200/api/search --data-urlencode 'q={ resource.service.name = "web" && kind = client }'
```

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
