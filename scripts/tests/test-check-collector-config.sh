#!/usr/bin/env bash
# Tests for scripts/ci/check-collector-config.sh (#1487): with --no-pull the
# guard must validate the locally present image and never `docker pull`, else the
# post-build step re-pulls the stale registry image over the HEAD build.
# `docker` is stubbed; it logs every invocation.
#
#   ./scripts/tests/test-check-collector-config.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CHECK="${REPO_ROOT}/scripts/ci/check-collector-config.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

mkdir "${WORK}/bin"
cat >"${WORK}/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"${DOCKER_LOG}"
case "$1" in
  pull) exit "${STUB_PULL_RC:-0}" ;;
  image) exit "${STUB_INSPECT_RC:-0}" ;;
  run) exit "${STUB_RUN_RC:-0}" ;;
esac
STUB
chmod +x "${WORK}/bin/docker"
export PATH="${WORK}/bin:${PATH}" DOCKER_LOG="${WORK}/docker.log"
config="${WORK}/config.yaml"
echo "receivers: {}" >"$config"

# run_check <want rc> <desc> <env assignments...> -- <check args...>
run_check() {
  local want="$1" desc="$2"
  shift 2
  local envs=()
  while [[ "$1" != "--" ]]; do envs+=("$1"); shift; done
  shift
  : >"$DOCKER_LOG"
  local rc=0
  env "${envs[@]}" bash "$CHECK" "$@" >"${WORK}/out" 2>&1 || rc=$?
  [[ "$rc" == "$want" ]] || { cat "${WORK}/out" >&2; fail "${desc}: want rc ${want}, got ${rc}"; }
  pass=$((pass + 1))
}

# --no-pull: validates the local image, never pulls (fails against a script that always pulls)
run_check 0 "no-pull accepts local image" STUB_PULL_RC=1 -- --no-pull example/otel:latest "$config"
if grep -q '^pull' "$DOCKER_LOG"; then fail "--no-pull ran docker pull"; fi
grep -q '^image inspect' "$DOCKER_LOG" || fail "--no-pull did not inspect the local image"
# --no-pull: image absent locally -> rc 2, still no pull
run_check 2 "no-pull, image absent" STUB_INSPECT_RC=1 -- --no-pull example/otel:latest "$config"
if grep -q '^pull' "$DOCKER_LOG"; then fail "--no-pull ran docker pull when image absent"; fi
# --no-pull: image rejects config -> rc 1
run_check 1 "no-pull, rejected config" STUB_RUN_RC=1 -- --no-pull example/otel:latest "$config"
# default mode still pulls first
run_check 0 "default pulls" -- example/otel:latest "$config"
grep -q '^pull' "$DOCKER_LOG" || fail "default mode did not pull"

echo "test-check-collector-config: ${pass} checks passed"
