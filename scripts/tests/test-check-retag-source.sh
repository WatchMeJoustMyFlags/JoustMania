#!/usr/bin/env bash
# Tests for scripts/ci/check-retag-source.sh (#1487): the retag-skipped job must
# refuse to copy IMAGE:main forward when :main predates the last commit that
# touched the image's sources. Uses SOURCE_REVISION / SOURCE_CREATED, so no
# registry or docker is needed.
#
#   ./scripts/tests/test-check-retag-source.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CHECK="${REPO_ROOT}/scripts/ci/check-retag-source.sh"
WORKFLOW="${REPO_ROOT}/.github/workflows/ci.yml"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# expect_rc <want rc> <description> <cmd...>
expect_rc() {
  local want="$1" desc="$2"
  shift 2
  local rc=0
  "$@" >"${WORK}/out" 2>&1 || rc=$?
  if [[ "$rc" != "$want" ]]; then
    cat "${WORK}/out" >&2
    fail "${desc}: want rc ${want}, got ${rc}"
  fi
  pass=$((pass + 1))
}

# 1. Every retagged image resolves to the same globs as its ci.yml filter.
for img in controller-manager-service game-coordinator-service menu-service audio-service \
  pairing-daemon-service connect-proxy dashboard rust-hid python-hid otel-collector flag-service; do
  out="$(CI_WORKFLOW="$WORKFLOW" "$CHECK" --paths "$img")" || fail "--paths ${img}"
  [[ -n "$out" ]] || fail "--paths ${img} printed nothing"
  pass=$((pass + 1))
done
grep -qx 'images/otel-collector/\*\*' <(CI_WORKFLOW="$WORKFLOW" "$CHECK" --paths otel-collector) ||
  fail "otel-collector globs miss images/otel-collector/**"
# *shared_python anchor is flattened into the service's globs
grep -qx 'lib/\*\*' <(CI_WORKFLOW="$WORKFLOW" "$CHECK" --paths menu-service) ||
  fail "menu-service globs miss the shared_python lib/**"
pass=$((pass + 2))

# Every image in ci.yml's retag list is known to the script (a new release image
# added to maybe_retag without a mapping would fail the retag job on main).
# shellcheck disable=SC2016 # literal $CH_ in the sed pattern
mapfile -t retag_images < <(sed -n 's/^ *maybe_retag "\$CH_[A-Z_]*" *\([a-z-]*\)$/\1/p' "$WORKFLOW")
[[ ${#retag_images[@]} -ge 11 ]] || fail "parsed only ${#retag_images[@]} maybe_retag images from ci.yml"
for img in "${retag_images[@]}"; do
  CI_WORKFLOW="$WORKFLOW" "$CHECK" --paths "$img" >/dev/null || fail "maybe_retag image ${img} unknown to the script"
  pass=$((pass + 1))
done

# 2. Freshness decisions in a scratch repo.
repo="${WORK}/repo"
mkdir -p "${repo}/.github/workflows" "${repo}/images/otel-collector" "${repo}/services/otel-collector"
cp "$WORKFLOW" "${repo}/.github/workflows/ci.yml"
git -C "$repo" init -q
git -C "$repo" config user.email t@example.com
git -C "$repo" config user.name test
commit() { # commit <file> <unix time>
  echo "$RANDOM" >>"${repo}/$1"
  git -C "$repo" add -A
  GIT_COMMITTER_DATE="@$2" GIT_AUTHOR_DATE="@$2" git -C "$repo" commit -qm "touch $1"
  git -C "$repo" rev-parse HEAD
}
old_build="$(commit images/otel-collector/Dockerfile 1700000000)"
source_change="$(commit images/otel-collector/builder-config.yaml 1700100000)"
# config.yaml is bind-mounted, not baked: not an image source
after_unrelated="$(commit services/otel-collector/config.yaml 1700200000)"

run() { (cd "$repo" && CI_WORKFLOW=.github/workflows/ci.yml "$CHECK" otel-collector); }
export -f run
export CHECK repo

# revision label: :main built before the last source change -> refuse (the #1487 incident)
expect_rc 1 "revision before source change" env SOURCE_REVISION="$old_build" SOURCE_CREATED= bash -c run
grep -q "STALE" "${WORK}/out" || fail "stale message missing"
# built at the source change, or later -> fresh
expect_rc 0 "revision at source change" env SOURCE_REVISION="$source_change" SOURCE_CREATED= bash -c run
expect_rc 0 "revision after source change" env SOURCE_REVISION="$after_unrelated" SOURCE_CREATED= bash -c run
# revision unknown to the checkout -> cannot verify, refuse
expect_rc 1 "unknown revision" env SOURCE_REVISION=0123456789abcdef0123456789abcdef01234567 SOURCE_CREATED= bash -c run
# no revision label: fall back to the creation time
expect_rc 1 "created before source change" env SOURCE_REVISION= SOURCE_CREATED="2023-11-15T00:00:00Z" bash -c run
expect_rc 0 "created after source change" env SOURCE_REVISION= SOURCE_CREATED="2023-11-17T00:00:00Z" bash -c run
expect_rc 1 "no label, no creation time" env SOURCE_REVISION= SOURCE_CREATED= bash -c run
# unknown image -> refuse
expect_rc 1 "unknown image" bash -c "cd '$repo' && '$CHECK' not-an-image"

echo "test-check-retag-source: ${pass} checks passed"
