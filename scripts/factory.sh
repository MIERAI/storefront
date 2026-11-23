#!/usr/bin/env bash
# api-factory as a tool, pinned: the v2 services' generated code comes from MIERAI/api-factory at FACTORY_SHA (no
# release tag yet; its module path is `apifactory`, so `go install` does not work: the factory runs from a checkout).
#
#   scripts/factory.sh services/catalog generate     design → check → prepare (rewrites the generated files)
#   scripts/factory.sh services/catalog verify       the same, then fails if any file differs from the commit (CI)
#   scripts/factory.sh services/catalog review [API]  the factory's spec review (AI, local claude CLI; all APIs, or
#                                                    one: create-product). Answer findings with
#                                                    scripts/factory.sh services/catalog answer design/catalog/X.yaml F1=keep:"…"
#   scripts/factory.sh services/catalog <any factory command> …
#
# Why the review is not in CI: it calls a model (credentials in a public repository's CI) and is not deterministic.
# It runs here when a design changes; its answers (design/<domain>/*.审查.yaml + the round ledgers *.审查轮次.jsonl) are
# committed, and CI checks that the generated files match the designs (verify).
#
# FACTORY_DIR: an existing api-factory checkout to use instead (must be at FACTORY_SHA unless FACTORY_ANY_SHA=1).
set -euo pipefail
FACTORY_SHA=7749483603bf9fcbf1b1c80e7827a23765c150f6   # api-factory main, 2026-11-23 (#45)
root=$(cd "$(dirname "$0")/.." && pwd)
project=$(cd "$root/${1:?usage: $0 services/<service> generate|verify|review|<factory command> …}" && pwd); shift
cmd=${1:?command}; shift

dir=${FACTORY_DIR:-$root/.cache/api-factory}
if [[ ! -d $dir/.git ]]; then
  git clone -q https://github.com/MIERAI/api-factory.git "$dir"
fi
if [[ $(git -C "$dir" rev-parse HEAD) != "$FACTORY_SHA" ]]; then
  if [[ -n ${FACTORY_DIR:-} && -z ${FACTORY_ANY_SHA:-} ]]; then
    echo "✗ $dir is not at $FACTORY_SHA (FACTORY_ANY_SHA=1 to use it anyway)" >&2; exit 1
  fi
  if [[ -z ${FACTORY_DIR:-} ]]; then
    git -C "$dir" fetch -q origin "$FACTORY_SHA" 2>/dev/null || git -C "$dir" fetch -q origin
    git -C "$dir" -c advice.detachedHead=false checkout -q "$FACTORY_SHA"
  fi
fi
F() { (cd "$dir" && go run ./factory/cmd/factory --project "$project" "$@"); }

case $cmd in
  generate)
    F design && F check && F prepare ;;
  verify)
    F design >/dev/null && F check >/dev/null && F prepare >/dev/null
    rel=${project#"$root"/}
    # against the index: what is committed (CI) or staged (locally); new untracked files count too
    changed=$(git -C "$root" diff --name-only -- "$rel"; git -C "$root" ls-files --others --exclude-standard -- "$rel")
    if [[ -n $changed ]]; then
      echo "✗ $rel: generated files differ from the designs (run scripts/factory.sh $rel generate and commit):"
      echo "$changed"; git -C "$root" --no-pager diff --stat -- "$rel"; exit 1
    fi
    echo "✓ $rel: generated files are up to date (api-factory ${FACTORY_SHA:0:7})" ;;
  accept)
    # the factory's acceptance (hand-written + automatic + differential cases) against the real server and an empty
    # database ACCEPT_DATABASE_URL (dropped and re-created tables: never point it at a real one), schema = db/schema
    url=${ACCEPT_DATABASE_URL:?ACCEPT_DATABASE_URL: an empty scratch database}
    (cd "$dir" && go build -o .factory/factory ./factory/cmd/factory)
    (cd "$project" && go build -o bin/server ./cmd/server)
    cat "$project"/db/schema/*.sql | psql -q -v ON_ERROR_STOP=1 "$url"
    (cd "$project" && "$dir/.factory/factory" serve-test --server bin/server --db "$url" -- "$dir/.factory/factory" --project . accept --db "$url") ;;
  review)
    specs=()
    if [[ $# -gt 0 ]]; then specs=("$project"/specs/*/"$1"/spec.yaml); else specs=("$project"/specs/*/*/spec.yaml); fi
    rc=0
    for s in "${specs[@]}"; do
      (cd "$dir" && PROJECT=$project scripts/review.sh "${s#"$project"/}") || rc=1
    done
    exit $rc ;;
  *)
    F "$cmd" "$@" ;;
esac
