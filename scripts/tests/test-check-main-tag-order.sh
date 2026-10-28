#!/usr/bin/env bash
# Tests for scripts/ci/check-main-tag-order.sh (#1793): an older main run must not
# move IMAGE:main / :latest back. Scratch git repo + a fake docker (registry).
# UC: none (CI-only)
#
#   ./scripts/tests/test-check-main-tag-order.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CHECK="${REPO_ROOT}/scripts/ci/check-main-tag-order.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0

fail() { echo "FAILED: $*" >&2; exit 1; }

# expect_rc <want> <desc> <cmd...>: runs from a NON-git cwd (the publish step runs in
# the digests dir), so the script must reach the repo through REPO_DIR.
expect_rc() {
  local want="$1" desc="$2" rc=0
  shift 2
  (cd "$nogit" && REPO_DIR="$repo" "$@") >"${WORK}/out" 2>&1 || rc=$?
  if [[ "$rc" != "$want" ]]; then
    cat "${WORK}/out" >&2
    fail "${desc}: want rc ${want}, got ${rc}"
  fi
  pass=$((pass + 1))
}
# expect_out <grep -E pattern> <desc>: the last run's output matches.
expect_out() { grep -qE -- "$1" "${WORK}/out" || { cat "${WORK}/out" >&2; fail "$2: output lacks /$1/"; }; pass=$((pass + 1)); }
# expect_no_out <grep -E pattern> <desc>: the last run's output does not match.
expect_no_out() { ! grep -qE -- "$1" "${WORK}/out" || { cat "${WORK}/out" >&2; fail "$2: output has /$1/"; }; pass=$((pass + 1)); }

nogit="${WORK}/nogit"
mkdir -p "$nogit"
if git -C "$nogit" rev-parse --git-dir >/dev/null 2>&1; then fail "${nogit} is inside a git repo; cannot test the non-git cwd"; fi

# Scratch repo with an origin (bare) whose main holds older/newer; side holds unrelated.
repo="${WORK}/repo"
mkdir -p "$repo"
git -C "$repo" init -q -b main
git -C "$repo" config user.email t@example.com
git -C "$repo" config user.name test
commit() { echo "$RANDOM" >>"${repo}/f"; git -C "$repo" add -A; git -C "$repo" commit -qm c; git -C "$repo" rev-parse HEAD; }
older="$(commit)"
newer="$(commit)"
git -C "$repo" checkout -q -b side "$older"
unrelated="$(commit)"
git -C "$repo" checkout -q main
git clone -q --bare "$repo" "${WORK}/origin.git"
git -C "$repo" remote add origin "${WORK}/origin.git"
# A main commit this checkout has not fetched yet (landed after the run's checkout).
git clone -q "${WORK}/origin.git" "${WORK}/other"
echo later >>"${WORK}/other/f"
git -C "${WORK}/other" -c user.email=t@example.com -c user.name=test commit -qam later
later="$(git -C "${WORK}/other" rev-parse HEAD)"
git -C "${WORK}/other" push -q origin main
unknown="1111111111111111111111111111111111111111"

# Label override (no registry).
expect_rc 0 "newer run, older :main" env SOURCE_REVISION="$older" "$CHECK" img "$newer"
expect_rc 0 "equal SHA (workflow_dispatch rerun)" env SOURCE_REVISION="$newer" "$CHECK" img "$newer"
expect_rc 3 "older run, newer :main" env SOURCE_REVISION="$newer" "$CHECK" img "$older"
expect_out "::notice::.*descends from" "older run, newer :main"
expect_rc 0 "unrelated :main revision moves" env SOURCE_REVISION="$unrelated" "$CHECK" img "$newer"
expect_out "::warning::.*unrelated" "unrelated :main revision"
expect_no_out "newer" "unrelated :main revision"
expect_rc 0 "no label on :main" env SOURCE_REVISION="" "$CHECK" img "$newer"
expect_rc 3 "newer :main not fetched yet (fetch finds it)" env SOURCE_REVISION="$later" "$CHECK" img "$newer"
expect_rc 0 "unknown revision after a successful fetch moves" env SOURCE_REVISION="$unknown" "$CHECK" img "$newer"
expect_out "::warning::.*not on origin/main" "unknown revision after fetch"
expect_no_out "newer" "unknown revision after fetch"
git -C "$repo" remote set-url origin "${WORK}/missing.git"
expect_rc 2 "fetch failure fails loud" env SOURCE_REVISION="$unknown" "$CHECK" img "$newer"
expect_out "::error::.*fetch" "fetch failure"
git -C "$repo" remote set-url origin "${WORK}/origin.git"
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
