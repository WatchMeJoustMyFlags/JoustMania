#!/usr/bin/env bash
# Check that the checkout's services/flag-service/registry.yaml loads with the
# flag-service of the release pinned in .env (IMAGE_TAG) (#2403).
#
# Why: compose mounts the checkout's registry.yaml into the flag-service image
# pinned by .env, and the released parser is strict (KnownFields). A new
# registry field therefore crashes every main checkout until a release ships a
# parser that accepts it (#2346 -> #2347, #2396). PR CI runs the PR's own image
# and cannot see this.
#
# How: build the tagged registry.LoadFile from a git worktree at the release
# tag (joustmania-v<IMAGE_TAG>) and run it on the checkout's registry.yaml and
# flag baseline (services/flagd, which compose mounts at /etc/flagd). No docker.
#
# Usage: scripts/ci/check-registry-pinned.sh [registry.yaml]
# Exit: 0 loads, 1 does not load, 2 setup problem (no tag, no go).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGISTRY="${1:-$ROOT/services/flag-service/registry.yaml}"
BASELINE_DIR="$ROOT/services/flagd"

IMAGE_TAG="$(sed -n 's/^IMAGE_TAG=\([^ #]*\).*/\1/p' "$ROOT/.env" | head -n1)"
if [[ -z "$IMAGE_TAG" ]]; then
  echo "ERROR: .env has no IMAGE_TAG; compose would not know which flag-service release to run" >&2
  exit 2
fi
if [[ "$IMAGE_TAG" == "latest" ]]; then
  echo "SKIP: .env pins IMAGE_TAG=latest, which builds the flag-service from this checkout" >&2
  exit 0
fi
TAG="joustmania-v${IMAGE_TAG}"

if ! git -C "$ROOT" rev-parse -q --verify "refs/tags/$TAG^{commit}" >/dev/null; then
  git -C "$ROOT" fetch -q --no-tags --depth=1 origin "refs/tags/$TAG:refs/tags/$TAG" \
    || { echo "ERROR: release tag $TAG not found locally or on origin" >&2; exit 2; }
fi

WORK="$(mktemp -d)"
trap 'git -C "$ROOT" worktree remove --force "$WORK/rel" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT
git -C "$ROOT" worktree add -q --detach "$WORK/rel" "refs/tags/$TAG"

CHECK_DIR="$WORK/rel/services/flag-service/zz_registrycheck"
mkdir -p "$CHECK_DIR"
cat >"$CHECK_DIR/main.go" <<'GO'
package main

import (
	"fmt"
	"os"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/registry"
)

func main() {
	b, err := baseline.Load(os.Args[1], baseline.DefaultFiles)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	r, err := registry.LoadFile(os.Args[2], b)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Printf("registry ok: %d policies\n", len(r.Policies()))
}
GO

# Build first, so a compile or download problem is a setup error (2), not "does not load".
(cd "$WORK/rel/services/flag-service" && go build -o "$WORK/registrycheck" ./zz_registrycheck) \
  || { echo "ERROR: could not build the registry check of $TAG" >&2; exit 2; }
set +e
"$WORK/registrycheck" "$BASELINE_DIR" "$REGISTRY"
rc=$?
set -e
if [[ $rc -eq 1 ]]; then
  cat >&2 <<MSG

FAIL: services/flag-service/registry.yaml does not load with the flag-service
of the pinned release ($TAG, .env IMAGE_TAG=$IMAGE_TAG).
Compose mounts this file next to that release image, so main checkouts would
crash on start. Use the two-step pattern (#2403):
  1. ship a tolerant parser (accepts the new field) in a release;
  2. bump IMAGE_TAG, then add the field to registry.yaml.
MSG
fi
exit $rc
