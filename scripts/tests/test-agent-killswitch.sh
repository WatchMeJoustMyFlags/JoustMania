#!/usr/bin/env bash
# Self-contained tests for scripts/agent-killswitch.sh (UC-045 main steps 1 and 7,
# A5 and A6: a refused or failed change leaves `enabled` as it was, Post-F-1).
#
# Runs the helper against scripts/tests/fake_flag_service.py serving
# services/flagd/ci/agent.json with the REAL registry (epic #1407): the helper
# changes `enabled` only through the flag service, never a flag file.
#
# Runs as part of `make lint` (needs only bash + python3), or directly:
#   ./scripts/tests/test-agent-killswitch.sh
#
# UC: UC-045 main A5 A6 Post-F-1
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HELPER="${SCRIPT_DIR}/../agent-killswitch.sh"
SHIPPED="${REPO_ROOT}/services/flagd/ci/agent.json"
WORK="$(mktemp -d)"
# shellcheck source=scripts/tests/lib-fake-flag-service.sh
source "${SCRIPT_DIR}/lib-fake-flag-service.sh"
trap 'stop_fake_flag_service; rm -rf "${WORK}"' EXIT
export FAKE_AGENT_FLAGS="${WORK}/agent.json"
cp "${SHIPPED}" "${FAKE_AGENT_FLAGS}"
start_fake_flag_service "${WORK}"

pass=0
fail=0
ok() { echo "ok   - $1"; pass=$((pass + 1)); }
not_ok() { echo "FAIL - $1"; fail=$((fail + 1)); }

# expect <name> <expected-exit> <expected-output-substring> -- <args...>
expect() {
  local name="$1" want_rc="$2" want_out="$3"
  shift 4
  local out rc=0
  out="$("${HELPER}" "$@" 2>&1)" || rc=$?
  if [[ "${rc}" -eq "${want_rc}" && "${out}" == *"${want_out}"* ]]; then
    ok "${name}"
  else
    not_ok "${name} (rc=${rc}, want ${want_rc}; output: ${out})"
  fi
}

effective() {
  curl -sS "${FLAG_SERVICE_URL}/agent/$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["effective_default"])'
}

# Every flag other than `enabled` still serves its baseline default.
others_unchanged() {
  curl -sS "${FLAG_SERVICE_URL}/agent" | python3 -c '
import json, sys
for view in json.load(sys.stdin)["flags"]:
    if view["flag"] != "enabled":
        assert view["effective_default"] == view["baseline_default"], view["flag"]
'
}

baseline="$(effective enabled)"
expect "status (no args) shows the current value" 0 "agent kill-switch = ${baseline}" --

# --- step 7: on ---------------------------------------------------------------
expect "on sets enabled on" 0 "agent kill-switch -> on" -- on
if [[ "$(effective enabled)" == "on" ]]; then ok "enabled == on"; else not_ok "enabled == on (got $(effective enabled))"; fi
if others_unchanged; then ok "on leaves every other flag unchanged"; else not_ok "on leaves every other flag unchanged"; fi
expect "status shows on" 0 "agent kill-switch = on" -- status
if grep -q "PUT /agent/enabled" "${FAKE_FLAG_LOG}"; then ok "on is a flag-service PUT"; else not_ok "on is a flag-service PUT"; fi

# --- step 1: off --------------------------------------------------------------
expect "off returns to the fail-closed baseline" 0 "agent kill-switch -> off (baseline)" -- off
if [[ "$(effective enabled)" == "off" ]]; then ok "enabled == off"; else not_ok "enabled == off (got $(effective enabled))"; fi
if others_unchanged; then ok "off leaves every other flag unchanged"; else not_ok "off leaves every other flag unchanged"; fi
if grep -q "^DELETE /agent/enabled" "${FAKE_FLAG_LOG}"; then ok "off is a flag-service DELETE"; else not_ok "off is a flag-service DELETE"; fi

# --- the baseline file is never written ---------------------------------------
if cmp -s "${SHIPPED}" "${FAKE_AGENT_FLAGS}"; then ok "the baseline file is unchanged"; else not_ok "the baseline file is unchanged"; fi

# --- rejections ----------------------------------------------------------------
expect "unknown action is rejected" 2 "usage:" -- maybe

# --- A5: the flag service refuses the off (503 no_sync_stream) -----------------
stop_fake_flag_service
start_fake_flag_service "${WORK}" --fail-delete 1
expect "A5 setup: on" 0 "agent kill-switch -> on" -- on
expect "A5 refused off prints the docker stop fallback" 1 "docker stop joustmania-agent" -- off
if [[ "$(effective enabled)" == "on" ]]; then ok "A5 refused off leaves enabled on"; else not_ok "A5 refused off leaves enabled on (got $(effective enabled))"; fi

# --- A6: the flag service refuses the on (503) ---------------------------------
stop_fake_flag_service
start_fake_flag_service "${WORK}" --fail-put 1
expect "A6 refused on prints the error" 1 "PUT /agent/enabled -> HTTP 503" -- on
if [[ "$(effective enabled)" == "off" ]]; then ok "A6 refused on leaves enabled off"; else not_ok "A6 refused on leaves enabled off (got $(effective enabled))"; fi

# --- A5 / A6: the flag service cannot be reached --------------------------------
stop_fake_flag_service
expect "A6 on prints the error when the flag service is unreachable" 1 "flag service unreachable" -- on
expect "A5 off prints the docker stop fallback when the flag service is unreachable" 1 "docker stop joustmania-agent" -- off

echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
