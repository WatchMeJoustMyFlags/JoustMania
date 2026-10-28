#!/usr/bin/env bash
# Tests for scripts/ci/check-retag-source.sh (#1487): the retag-skipped job must
# refuse to copy IMAGE:main forward when :main predates the last commit that
# touched the image's sources. Uses SOURCE_REVISION / SOURCE_CREATED, so no
# registry or docker is needed.
#
#   ./scripts/tests/test-check-retag-source.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

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
  pairing-daemon-service connect-proxy dashboard rust-hid python-hid operator-pad otel-collector flag-service agent; do
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

# registry path: labels come from `buildx imagetools inspect` (no pull), amd64 entry of an index
mkdir "${WORK}/bin"
cat >"${WORK}/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"${DOCKER_LOG}"
[[ "$1 $2 $3" == "buildx imagetools inspect" ]] || exit 99
if [[ -n "${STUB_ERR:-}" ]]; then echo "$STUB_ERR" >&2; exit 1; fi
printf '%s' "${STUB_CONFIG}"
STUB
chmod +x "${WORK}/bin/docker"
export DOCKER_LOG="${WORK}/docker.log" PATH="${WORK}/bin:${PATH}"
: >"$DOCKER_LOG"
export STUB_CONFIG="{\"linux/amd64\":{\"created\":\"2023-11-17T00:00:00Z\",\"config\":{\"Labels\":{\"org.opencontainers.image.revision\":\"${old_build}\"}}}}"
expect_rc 1 "registry labels: stale revision" bash -c "cd '$repo' && IMAGE_PREFIX=x/y CI_WORKFLOW=.github/workflows/ci.yml '$CHECK' otel-collector"
grep -q "STALE" "${WORK}/out" || fail "registry stale message missing"
grep -q "in-flight main run" "${WORK}/out" || fail "in-flight hint missing"
export STUB_CONFIG="{\"created\":\"2023-11-17T00:00:00Z\",\"config\":{}}"
expect_rc 0 "registry no label: creation time fallback" bash -c "cd '$repo' && IMAGE_PREFIX=x/y CI_WORKFLOW=.github/workflows/ci.yml '$CHECK' otel-collector"
if grep -q '^pull' "$DOCKER_LOG"; then fail "retag check pulled the image"; fi

# #2120: a registry error is not a missing tag (exit 1), a real not-found is exit 2.
run_reg() { bash -c "cd '$repo' && IMAGE_PREFIX=x/y CI_WORKFLOW=.github/workflows/ci.yml '$CHECK' otel-collector"; }
expect_rc 2 "registry not found -> exit 2" env STUB_ERR="ghcr.io/x/y:main: not found" bash -c "$(declare -f run_reg); repo='$repo' CHECK='$CHECK' run_reg"
expect_rc 1 "registry auth error -> exit 1" env STUB_ERR="denied: requested access to the resource is denied" bash -c "$(declare -f run_reg); repo='$repo' CHECK='$CHECK' run_reg"
expect_rc 1 "port 404 error -> exit 1" env STUB_ERR="dial tcp 10.0.4.404:5000: connection refused" bash -c "$(declare -f run_reg); repo='$repo' CHECK='$CHECK' run_reg"
expect_rc 1 "missing credential helper -> exit 1" env STUB_ERR='exec: "docker-credential-x": executable file not found in $PATH' bash -c "$(declare -f run_reg); repo='$repo' CHECK='$CHECK' run_reg"
grep -q "registry error" "${WORK}/out" || fail "registry error message missing"

echo "test-check-retag-source: ${pass} checks passed"
