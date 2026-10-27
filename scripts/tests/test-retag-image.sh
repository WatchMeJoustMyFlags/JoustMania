#!/usr/bin/env bash
# UC: UC-060
# Tests for scripts/ci/retag-image.sh (#2120): the retag-skipped job must fail
# when IMAGE:<sha> does not resolve to :main's digest after the copy. Uses a
# fake `docker` (DOCKER=...) backed by a directory of "tags", no registry.
#
#   ./scripts/tests/test-retag-image.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RETAG="${SCRIPT_DIR}/../ci/retag-image.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0
fail() { echo "FAIL: $*" >&2; exit 1; }

# Fake registry: ${REG}/<ref with / and : replaced by _> holds a digest.
# FAKE_COPY=skip: `create` reports success but writes nothing (the #2120 shape);
# FAKE_COPY=fail: `create` exits 1.
cat >"${WORK}/docker" <<'FAKE'
#!/usr/bin/env bash
key() { echo "${1//[\/:@]/_}"; }
case "$3" in
  inspect)
    f="${REG}/$(key "$4")"
    [[ -f "$f" ]] && cat "$f" || exit 1 ;;
  create)
    [[ "${FAKE_COPY:-ok}" == fail ]] && exit 1
    [[ "${FAKE_COPY:-ok}" == skip ]] && exit 0
    [[ "${FAKE_COPY:-ok}" == wrong ]] && { echo sha256:bad >"${REG}/$(key "$5")"; exit 0; }
    src="$6"; digest="${src##*@}"
    echo "$digest" >"${REG}/$(key "$5")"
    ;;
esac
FAKE
chmod +x "${WORK}/docker"

PFX=ghcr.io/o/r
run() { # run <want rc> <desc> [env...]
  local want="$1" desc="$2"; shift 2
  local rc=0
  env REG="${WORK}/reg" DOCKER="${WORK}/docker" IMAGE_PREFIX="$PFX" TARGET_SHA=abc123 \
    VERIFY_ATTEMPTS=2 VERIFY_SLEEP=0 "$@" bash "$RETAG" menu-service >"${WORK}/out" 2>&1 || rc=$?
  [[ "$rc" == "$want" ]] || { cat "${WORK}/out" >&2; fail "${desc}: want rc ${want}, got ${rc}"; }
  pass=$((pass + 1))
}
reset() { rm -rf "${WORK}/reg"; mkdir -p "${WORK}/reg"; }
set_main() { echo "sha256:feed" >"${WORK}/reg/ghcr.io_o_r_menu-service_main"; }

reset; set_main
run 0 "copy lands and verifies"
[[ -f "${WORK}/reg/ghcr.io_o_r_menu-service_abc123" ]] || fail "target tag not written"

# UC-060: a copy that reports success but writes nothing must fail the job.
reset; set_main
run 1 "silent skip must fail" FAKE_COPY=skip
grep -q "does not resolve" "${WORK}/out" || fail "no verification error message"

reset; set_main
run 1 "wrong digest must fail" FAKE_COPY=wrong

reset; set_main
run 1 "copy failure must fail" FAKE_COPY=fail

reset
run 2 "missing :main is exit 2 (caller warns)"

echo "OK: ${pass} checks"
