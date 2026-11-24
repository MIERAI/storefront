#!/usr/bin/env bash
# api-factory as a tool, pinned: the v2 services' generated code comes from MIERAI/api-factory release FACTORY_VERSION
# (tag; FACTORY_SHA is the commit it must resolve to: a tag that moved fails). The factory's module path is
# `apifactory`, so `go install` does not work: it runs from a checkout. One version for the factory and its runtime:
# factory vX.Y.Z generates code for runtime vX.Y.Z (github.com/MIERAI/api-factory/runtime, services/*/go.mod).
#
#   scripts/factory.sh services/catalog upgrade      the service's runtime → the one paired with FACTORY_VERSION
#                                                    (go.mod + imports, go mod tidy, go build); then generate
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
#
# Between releases FACTORY_VERSION may be a Go pseudo-version of an untagged api-factory commit (what
# `go list -m github.com/MIERAI/api-factory/runtime@<sha>` prints): its last 12 hex digits must be FACTORY_SHA's
# prefix; the commit is fetched directly, and FACTORY_RUNTIME (read by `factory upgrade` and `prepare`) pins the
# services' runtime to that same pseudo-version. Back to a release = a tag here again.
set -euo pipefail
# api-factory release v0.7.0 (tags v0.7.0 + runtime/v0.7.0): the runtime with OpenTelemetry tracing (MIERAI/api-factory#62)
FACTORY_VERSION=v0.7.0
FACTORY_SHA=85de05d39229ecb3d2a6b9be1ee42533e7a19a77
pseudo=
if [[ $FACTORY_VERSION =~ -0\.[0-9]{14}-([0-9a-f]{12})$ ]]; then
  [[ ${BASH_REMATCH[1]} == "${FACTORY_SHA:0:12}" ]] || { echo "✗ FACTORY_VERSION $FACTORY_VERSION is not FACTORY_SHA ${FACTORY_SHA:0:12}" >&2; exit 1; }
  pseudo=1
  export FACTORY_RUNTIME=$FACTORY_VERSION
fi
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
  if [[ -z ${FACTORY_DIR:-} && -n $pseudo ]]; then
    git -C "$dir" fetch -q origin "$FACTORY_SHA"   # an untagged commit: by its id
    git -C "$dir" -c advice.detachedHead=false checkout -q "$FACTORY_SHA"
  elif [[ -z ${FACTORY_DIR:-} ]]; then
    git -C "$dir" fetch -q origin "refs/tags/$FACTORY_VERSION:refs/tags/$FACTORY_VERSION" 2>/dev/null || git -C "$dir" fetch -q --tags origin
    tagged=$(git -C "$dir" rev-parse "$FACTORY_VERSION^{commit}")
    [[ $tagged == "$FACTORY_SHA" ]] || { echo "✗ api-factory tag $FACTORY_VERSION is $tagged, pinned $FACTORY_SHA" >&2; exit 1; }
    git -C "$dir" -c advice.detachedHead=false checkout -q "$FACTORY_SHA"
  fi
fi
F() { (cd "$dir" && go run ./factory/cmd/factory --project "$project" "$@"); }

case $cmd in
  generate)
    F design && F check && F prepare ;;
  verify)
    F upgrade --check >/dev/null || { F upgrade --check; exit 1; }   # go.mod: the runtime paired with the factory
    F design >/dev/null && F check >/dev/null && F prepare >/dev/null
    rel=${project#"$root"/}
    # against the index: what is committed (CI) or staged (locally); new untracked files count too
    changed=$(git -C "$root" diff --name-only -- "$rel"; git -C "$root" ls-files --others --exclude-standard -- "$rel")
    if [[ -n $changed ]]; then
      echo "✗ $rel: generated files differ from the designs (run scripts/factory.sh $rel generate and commit):"
      echo "$changed"; git -C "$root" --no-pager diff --stat -- "$rel"; exit 1
    fi
    echo "✓ $rel: generated files are up to date (api-factory $FACTORY_VERSION, ${FACTORY_SHA:0:7})" ;;
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
