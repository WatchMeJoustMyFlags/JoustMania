#!/usr/bin/env bash
# Self-contained tests for scripts/ci/apt-install.sh (#2494): update retried until
# it succeeds, gives up after 3 failed tries, install flags and options passed.
#
# Runs as part of `make lint`, or directly: ./scripts/tests/test-apt-install.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APT="${SCRIPT_DIR}/../ci/apt-install.sh"
WORK="$(realpath "$(mktemp -d)")"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
# Stub apt-get: `update` fails while the count file is below FAIL_UPDATES.
cat >"${WORK}/bin/apt-get" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"${WORK}/calls"
if [[ " $* " == *" update "* ]]; then
  n=$(($(cat "${WORK}/n" 2>/dev/null || echo 0) + 1))
  echo "${n}" >"${WORK}/n"
  [[ "${n}" -le "${FAIL_UPDATES}" ]] && exit 100
fi
exit 0
STUB
chmod +x "${WORK}/bin/apt-get"
export WORK PATH="${WORK}/bin:${PATH}" APT_SUDO="" APT_RETRY_SLEEP=0

fail() { echo "FAIL: $*" >&2; exit 1; }
reset() { rm -f "${WORK}/calls" "${WORK}/n"; }

# Succeeds on try 2: two updates, one install.
reset
FAIL_UPDATES=1 "${APT}" --no-install-recommends pkg-a pkg-b >/dev/null 2>&1 || fail "try 2 should succeed"
[[ "$(grep -c ' update ' "${WORK}/calls")" == 2 ]] || fail "expected 2 update calls"
grep -q 'install -y -q --no-install-recommends pkg-a pkg-b' "${WORK}/calls" || fail "install args"
grep -q 'Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30' "${WORK}/calls" || fail "acquire options"

# Fails 3 times: non-zero, no install.
reset
if FAIL_UPDATES=99 "${APT}" pkg-a >/dev/null 2>&1; then fail "3 failures should exit non-zero"; fi
[[ "$(grep -c ' update ' "${WORK}/calls")" == 3 ]] || fail "expected 3 update calls"
! grep -q ' install ' "${WORK}/calls" || fail "must not install after failed update"

# No packages: usage error.
reset
if FAIL_UPDATES=0 "${APT}" >/dev/null 2>&1; then fail "no packages should fail"; fi

echo "OK"
