#!/usr/bin/env bash
# Tests for scripts/ci/release-retag.sh (#2338): release.yml tag-images must wait, bounded,
# for IMAGE:<tag sha> (the tag commit's CI may still be queued), keep the no-overwrite and
# fail-closed guards, and name the dispatch command on timeout. Fake `docker` backed by a
# directory of "tags" and a fake `sleep` that only records; no registry, no real waiting.
#
#   ./scripts/tests/test-release-retag.sh
# UC: none (CI-only)
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RETAG="${SCRIPT_DIR}/../ci/release-retag.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0
fail() { echo "FAIL: $*" >&2; exit 1; }
mkdir -p "${WORK}/bin"

# Fake registry: ${REG}/<ref with / : @ replaced by _> holds a digest.
# FAKE_SRC_AFTER=n: the :<sha> tag appears on the (n+1)th inspect of it (CI still running).
# FAKE_ERR=text: every inspect fails with that stderr (auth / registry error).
cat >"${WORK}/bin/docker" <<'FAKE'
#!/usr/bin/env bash
key() { echo "${1//[\/:@]/_}"; }
case "$3" in
  inspect)
    [[ -n "${FAKE_ERR:-}" ]] && { echo "$FAKE_ERR" >&2; exit 1; }
    if [[ "$4" == *":${SHA}" && -n "${FAKE_SRC_AFTER:-}" ]]; then
      n=$(cat "${REG}/.ic" 2>/dev/null || echo 0); echo $((n + 1)) >"${REG}/.ic"
      ((n >= FAKE_SRC_AFTER)) && echo "sha256:feed" >"${REG}/$(key "$4")"
    fi
    f="${REG}/$(key "$4")"
    if [[ -f "$f" ]]; then cat "$f"; else echo "ERROR: $4: not found" >&2; exit 1; fi ;;
  create)
    echo "$6" >>"${REG}/.created"
    [[ "${FAKE_COPY:-ok}" == skip ]] && exit 0
    echo "${6##*@}" >"${REG}/$(key "$5")" ;;
esac
FAKE
# Fake sleep: record the duration, do not wait.
cat >"${WORK}/bin/sleep" <<'FAKE'
#!/usr/bin/env bash
echo "$1" >>"${REG}/.slept"
FAKE
chmod +x "${WORK}/bin/docker" "${WORK}/bin/sleep"

PFX=ghcr.io/o/r
SHA=0123456789abcdef0123456789abcdef01234567
run() { # run <want rc> <desc> [env...]
  local want="$1" desc="$2"; shift 2
  local rc=0
  env PATH="${WORK}/bin:${PATH}" REG="${WORK}/reg" DOCKER="${WORK}/bin/docker" IMAGE_PREFIX="$PFX" \
    VERSION=0.9.1 SHA="$SHA" REPO=o/r WAIT_TIMEOUT=2700 WAIT_SLEEP=20 WAIT_MAX_SLEEP=300 \
    VERIFY_ATTEMPTS=2 VERIFY_SLEEP=0 "$@" bash "$RETAG" menu-service >"${WORK}/out" 2>&1 || rc=$?
  [[ "$rc" == "$want" ]] || { cat "${WORK}/out" >&2; fail "${desc}: want rc ${want}, got ${rc}"; }
  pass=$((pass + 1))
}
reset() { rm -rf "${WORK}/reg"; mkdir -p "${WORK}/reg"; }
src_file="${WORK}/reg/ghcr.io_o_r_menu-service_${SHA}"
dst_file="${WORK}/reg/ghcr.io_o_r_menu-service_0.9.1"
set_src() { echo "sha256:feed" >"$src_file"; }
slept_total() { awk '{s += $1} END {print s + 0}' "${WORK}/reg/.slept" 2>/dev/null || echo 0; }

reset; set_src
run 0 "source present: retag at once"
[[ "$(cat "$dst_file")" == sha256:feed ]] || fail "version tag not written with the source digest"
[[ ! -f "${WORK}/reg/.slept" ]] || fail "slept although the source existed"
grep -qx "${PFX}/menu-service@sha256:feed" "${WORK}/reg/.created" || fail "copy not by digest"

# The 0.9.0 shape: the tag commit's CI is still queued, :<sha> appears later.
reset
run 0 "source appears after 3 polls: wait then retag" FAKE_SRC_AFTER=3
[[ "$(cat "$dst_file")" == sha256:feed ]] || fail "version tag not written after waiting"
[[ "$(tr '\n' ' ' <"${WORK}/reg/.slept")" == "20 40 80 " ]] || fail "backoff not 20/40/80: $(cat "${WORK}/reg/.slept")"

reset
run 1 "source never appears: bounded wait, then fail"
[[ "$(slept_total)" == 2700 ]] || fail "waited $(slept_total)s, want exactly the 2700s bound"
awk '$1 > 300 {exit 1}' "${WORK}/reg/.slept" || fail "a backoff step exceeded the 300s cap"
grep -q "menu-service:${SHA} still missing" "${WORK}/out" || fail "timeout message does not name the image"
grep -qF "gh workflow run release.yml --repo o/r -f version=0.9.1 -f sha=${SHA}" "${WORK}/out" ||
  fail "timeout message lacks the dispatch command"
[[ ! -f "${WORK}/reg/.created" ]] || fail "created a tag without a source"

reset
run 1 "auth error on the source fails closed, no wait" FAKE_ERR="unauthorized: authentication required"
[[ ! -f "${WORK}/reg/.slept" && ! -f "${WORK}/reg/.created" ]] || fail "waited or created on an auth error"
grep -q "failing closed" "${WORK}/out" || fail "no fail-closed message"

reset
# shellcheck disable=SC2016 # literal $PATH: the text a missing credential helper prints
run 1 "missing credential helper is not a missing tag" \
  FAKE_ERR='error getting credentials: exec: "docker-credential-x": executable file not found in $PATH'
[[ ! -f "${WORK}/reg/.slept" ]] || fail "waited on a credential-helper error"

reset; set_src; echo "sha256:feed" >"$dst_file"
run 0 "version tag already at the source digest: nothing to do"
[[ ! -f "${WORK}/reg/.created" ]] || fail "recreated an identical tag"

reset; set_src; echo "sha256:other" >"$dst_file"
run 1 "version tag with another digest is never overwritten"
[[ "$(cat "$dst_file")" == sha256:other && ! -f "${WORK}/reg/.created" ]] || fail "overwrote the version tag"
grep -q "not overwriting" "${WORK}/out" || fail "no overwrite message"

reset; set_src
run 1 "copy that never lands fails" FAKE_COPY=skip
grep -q "does not resolve" "${WORK}/out" || fail "no verification message"

echo "OK: ${pass} checks"
