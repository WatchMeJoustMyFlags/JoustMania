#!/usr/bin/env bash
# Self-contained tests for scripts/ci/docker-pull-retry.sh (#2551): a pull that fails
# twice then succeeds passes, one that always fails exits non-zero after 3 tries,
# compose:<service> resolves the image from the compose file.
#
# Runs as part of `make lint`, or directly: ./scripts/tests/test-docker-pull-retry.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PULL="${SCRIPT_DIR}/../ci/docker-pull-retry.sh"
WORK="$(realpath "$(mktemp -d)")"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
# Stub docker: `pull` fails while the count file is below FAIL_PULLS.
cat >"${WORK}/bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"${WORK}/calls"
n=$(($(cat "${WORK}/n" 2>/dev/null || echo 0) + 1))
echo "${n}" >"${WORK}/n"
[[ "${n}" -le "${FAIL_PULLS}" ]] && exit 1
exit 0
STUB
chmod +x "${WORK}/bin/docker"
cat >"${WORK}/compose.yml" <<'YML'
services:
  # comment
  envoy:
    image: example/envoy:v9
    ports: []
  other:
    image: example/other:v1
YML
export WORK PATH="${WORK}/bin:${PATH}" PULL_RETRY_SLEEP=0 COMPOSE_FILE="${WORK}/compose.yml"

fail() { echo "FAIL: $*" >&2; exit 1; }
reset() { rm -f "${WORK}/calls" "${WORK}/n"; }

# Fails twice, succeeds on try 3.
reset
FAIL_PULLS=2 "${PULL}" example/img:1 >/dev/null 2>&1 || fail "try 3 should succeed"
[[ "$(grep -c 'pull' "${WORK}/calls")" == 3 ]] || fail "expected 3 pull calls"

# Always fails: non-zero after exactly 3 tries.
reset
if FAIL_PULLS=99 "${PULL}" example/img:1 >/dev/null 2>&1; then fail "3 failures should exit non-zero"; fi
[[ "$(grep -c 'pull' "${WORK}/calls")" == 3 ]] || fail "expected 3 pull calls before giving up"

# compose:<service> resolves the compose image, several images in one call.
reset
FAIL_PULLS=0 "${PULL}" compose:envoy example/x:2 >/dev/null 2>&1 || fail "compose pull"
grep -q 'pull -q example/envoy:v9' "${WORK}/calls" || fail "compose image not pulled"
grep -q 'pull -q example/x:2' "${WORK}/calls" || fail "second image not pulled"

# Unknown compose service and no args: usage errors, no pull.
reset
if FAIL_PULLS=0 "${PULL}" compose:nope >/dev/null 2>&1; then fail "unknown service should fail"; fi
if FAIL_PULLS=0 "${PULL}" >/dev/null 2>&1; then fail "no args should fail"; fi
[[ ! -e "${WORK}/calls" ]] || fail "must not pull on usage error"

echo "OK"
