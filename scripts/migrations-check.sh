#!/usr/bin/env bash
# Versioned migrations of a factory-generated v2 service (goose, embedded: <service>/internal/migrations, run by
# `/server migrate`). Adapted from api-factory scripts/ci/migrations-check.sh:
#   1. names NNNNN_<snake_case>.sql, versions strictly increasing; every file has "-- +goose Up" and "-- +goose Down"
#   2. append-only: a migration already on BASE is never edited or deleted (it may have run in prod: fix forward)
#   3. drift: all migrations on an empty database == db/schema/*.sql on another (pg_dump --schema-only). The factory
#      reads db/schema (its input), the clusters run the migrations: a change must be made in both
#   4. a second run (a restarted init container) applies nothing
# usage: scripts/migrations-check.sh services/catalog    BASE (default origin/main). Needs docker and go.
set -euo pipefail
cd "$(dirname "$0")/.."
svc=${1:?usage: $0 services/<service>}
dir=$svc/internal/migrations
BASE=${BASE:-origin/main}
fail=0
prev=0
for f in "$dir"/*.sql; do
  n=$(basename "$f")
  [[ $n =~ ^([0-9]{5})_[a-z0-9_]+\.sql$ ]] || { echo "✗ $n: name must be NNNNN_snake_case.sql"; fail=1; continue; }
  v=$((10#${BASH_REMATCH[1]}))
  (( v > prev )) || { echo "✗ $n: version $v not greater than $prev"; fail=1; }
  prev=$v
  grep -q '^-- +goose Up' "$f" || { echo "✗ $n: no '-- +goose Up'"; fail=1; }
  grep -q '^-- +goose Down' "$f" || { echo "✗ $n: no '-- +goose Down' (write the rollback, or an explicit no-op with a reason)"; fail=1; }
done
if git rev-parse -q --verify "$BASE" >/dev/null; then
  while read -r st path; do
    case $st in A) ;; *) echo "✗ $path: already on $BASE ($st): migrations are append-only, add a new one"; fail=1 ;; esac
  done < <(git -c core.quotePath=false diff --name-status "$BASE" -- "$dir/*.sql")
fi
(( fail == 0 )) || exit 1
echo "✓ $svc migrations: $(ls "$dir"/*.sql | wc -l | tr -d ' ') files, versions increasing, Up + Down, append-only"

name=sf-drift-$$
work=$(mktemp -d)
docker run -d --rm --name "$name" -e POSTGRES_PASSWORD=t -e POSTGRES_USER=svc -e POSTGRES_DB=svc "${IMAGE_MIRROR:-mirror.gcr.io/library}/postgres:16-alpine" >/dev/null
trap 'docker rm -f "$name" >/dev/null; rm -rf "$work"' EXIT
until docker exec "$name" pg_isready -q -U svc 2>/dev/null; do sleep 1; done; sleep 1
psql() { docker exec -i -e PGOPTIONS="-c client_min_messages=warning" "$name" psql -q -U svc -v ON_ERROR_STOP=1 "$@"; }
psql -d svc -c "CREATE DATABASE from_migrations" -c "CREATE DATABASE from_schema"
cat "$svc"/db/schema/*.sql | psql -d from_schema >/dev/null
# the migrations through the real binary (the init container runs the same), inside the container's network namespace
arch=$(docker exec "$name" uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/')
(cd "$svc" && GOOS=linux GOARCH=$arch CGO_ENABLED=0 go build -o "$work/server" ./cmd/server)
docker cp "$work/server" "$name":/tmp/server >/dev/null
url="postgres://svc:t@localhost:5432/from_migrations?sslmode=disable"
docker exec -e DATABASE_URL="$url" "$name" /tmp/server migrate up
docker exec -e DATABASE_URL="$url" "$name" /tmp/server migrate up | tee "$work/second.log" | grep -q '(0 applied' \
  || { echo "✗ a second run applied migrations again:"; cat "$work/second.log"; exit 1; }
echo "✓ a second run applies nothing"
dump() { docker exec "$name" pg_dump -U svc --schema-only --no-owner --no-privileges -T goose_db_version -T goose_db_version_id_seq "$1" | grep -vE '^(--|SET |SELECT pg_catalog|\\(un)?restrict )' | sed '/^$/d'; }
if diff <(dump from_schema) <(dump from_migrations) > "$work/drift.diff"; then
  echo "✓ schema drift: $dir == $svc/db/schema"
else
  echo "✗ schema drift: $dir and $svc/db/schema differ (< db/schema, > migrations):"; head -40 "$work/drift.diff"; exit 1
fi
# data migrations: what the database holds afterwards (the log of a CI run shows it). Only catalog has one (00003)
if [[ $svc == services/catalog ]]; then
  psql -d from_migrations -c "SELECT sku, name, category, price, status, version FROM products ORDER BY sku"
fi
