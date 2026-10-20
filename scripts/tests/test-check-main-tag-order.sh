#!/usr/bin/env bash
# Tests for scripts/ci/check-main-tag-order.sh (#1793): an older main run must not
# move IMAGE:main / :latest back. Scratch git repo + a fake docker (registry).
# UC: none (CI-only)
#
#   ./scripts/tests/test-check-main-tag-order.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CHECK="${REPO_ROOT}/scripts/ci/check-main-tag-order.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0

fail() { echo "FAIL: $*" >&2; exit 1; }

# expect_rc <want> <desc> <cmd...> (runs inside the scratch repo)
expect_rc() {
  local want="$1" desc="$2" rc=0
  shift 2
  (cd "$repo" && "$@") >"${WORK}/out" 2>&1 || rc=$?
  if [[ "$rc" != "$want" ]]; then
    cat "${WORK}/out" >&2
    fail "${desc}: want rc ${want}, got ${rc}"
  fi
  pass=$((pass + 1))
}

repo="${WORK}/repo"
mkdir -p "$repo"
git -C "$repo" init -q
git -C "$repo" config user.email t@example.com
git -C "$repo" config user.name test
commit() { echo "$RANDOM" >>"${repo}/f"; git -C "$repo" add -A; git -C "$repo" commit -qm c; git -C "$repo" rev-parse HEAD; }
older="$(commit)"
newer="$(commit)"
git -C "$repo" checkout -q -b side "$older"
unrelated="$(commit)"
git -C "$repo" checkout -q -

# Label override (no registry).
expect_rc 0 "newer run, older :main" env SOURCE_REVISION="$older" "$CHECK" img "$newer"
expect_rc 0 "equal SHA (workflow_dispatch rerun)" env SOURCE_REVISION="$newer" "$CHECK" img "$newer"
expect_rc 3 "older run, newer :main" env SOURCE_REVISION="$newer" "$CHECK" img "$older"
expect_rc 3 "unrelated :main revision" env SOURCE_REVISION="$unrelated" "$CHECK" img "$newer"
expect_rc 0 "no label on :main" env SOURCE_REVISION="" "$CHECK" img "$newer"
expect_rc 3 "unknown revision (newer than this checkout)" env SOURCE_REVISION="1111111111111111111111111111111111111111" "$CHECK" img "$newer"
expect_rc 1 "missing sha" env -u GITHUB_SHA SOURCE_REVISION="$older" "$CHECK" img

# Registry path through a fake docker: multi-arch index and missing tag.
fake="${WORK}/bin"
mkdir -p "$fake"
cat >"${fake}/docker" <<'DOCKER'
#!/bin/sh
[ -n "$FAKE_CONFIG" ] || exit 1
printf '%s' "$FAKE_CONFIG"
DOCKER
chmod +x "${fake}/docker"
index() { printf '{"linux/amd64":{"config":{"Labels":{"org.opencontainers.image.revision":"%s"}}},"linux/arm64":{"config":{"Labels":{"org.opencontainers.image.revision":"%s"}}}}' "$1" "$1"; }
reg() { env PATH="${fake}:${PATH}" IMAGE_PREFIX=ghcr.io/o/r "$@"; }
expect_rc 0 "index, newer run" reg FAKE_CONFIG="$(index "$older")" "$CHECK" img "$newer"
expect_rc 3 "index, older run" reg FAKE_CONFIG="$(index "$newer")" "$CHECK" img "$older"
expect_rc 0 "single image, equal" reg FAKE_CONFIG="{\"config\":{\"Labels\":{\"org.opencontainers.image.revision\":\"$newer\"}}}" "$CHECK" img "$newer"
expect_rc 0 "missing :main" reg FAKE_CONFIG="" "$CHECK" img "$newer"

echo "PASS: ${pass} checks"
