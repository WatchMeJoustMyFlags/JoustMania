#!/usr/bin/env bash
# Self-contained tests for scripts/agent-dryrun-enable.sh (FR-090, #1907): `on`
# sets the experiment_seed flag to its windows_frantic variant BEFORE the
# `enabled` and `experiments_enabled` gates, `off` clears all three, and
# `status` shows them.
#
# Runs the helper against scripts/tests/fake_flag_service.py serving
# services/flagd/ci/agent.json with the REAL registry (epic #1407): the helper
# changes the flags only through the flag service, never a flag file.
#
# Runs as part of `make lint` (needs only bash + python3), or directly:
#   ./scripts/tests/test-agent-dryrun-enable.sh
#
# UC: UC-070 BR-007
# UC: UC-043 A16
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HELPER="${SCRIPT_DIR}/../agent-dryrun-enable.sh"
SHIPPED="${REPO_ROOT}/services/flagd/ci/agent.json"
unset COMPOSE DOCKER || true
# A test that reaches for the real docker fails instead of using it
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

check() { # check <name> <flag> <want>
  local got
  got="$(effective "$2")"
  if [[ "${got}" == "$3" ]]; then ok "$1"; else not_ok "$1 (got ${got}, want $3)"; fi
}

# Line number of the first request matching a regex in the fake's request log.
line_of() { { grep -n -m1 -E "$1" "${FAKE_FLAG_LOG}" || true; } | cut -d: -f1; }

check "baseline: experiment_seed is off" experiment_seed off
expect "status shows experiment_seed" 0 "experiment_seed = off" -- status
expect "status shows decision.backend" 0 "decision.backend = " -- status

# --- on: the seed, then the gates ----------------------------------------------
expect "on reports the seed" 0 "experiment_seed -> windows_frantic" -- on
check "on sets experiment_seed to windows_frantic" experiment_seed windows_frantic
check "on sets enabled on" enabled on
check "on sets experiments_enabled on" experiments_enabled on
seed_line="$(line_of '^PUT /agent/experiment_seed ')"
enabled_line="$(line_of '^PUT /agent/enabled ')"
gate_line="$(line_of '^PUT /agent/experiments_enabled ')"
if [[ -n "${seed_line}" && -n "${enabled_line}" && -n "${gate_line}" &&
  "${seed_line}" -lt "${enabled_line}" && "${seed_line}" -lt "${gate_line}" ]]; then
  ok "on writes the seed before enabled and experiments_enabled"
else
  not_ok "on writes the seed before the gates (seed=${seed_line} enabled=${enabled_line} experiments_enabled=${gate_line})"
fi
expect "status shows the seed variant" 0 "experiment_seed = windows_frantic" -- status

# --- off: all three back to the baseline -----------------------------------------
expect "off reports the seed baseline" 0 "experiment_seed -> off (baseline)" -- off
check "off clears experiment_seed" experiment_seed off
check "off clears enabled" enabled off
check "off clears experiments_enabled" experiments_enabled off
if grep -q "^DELETE /agent/experiment_seed" "${FAKE_FLAG_LOG}"; then ok "off is a flag-service DELETE of the seed"; else not_ok "off is a flag-service DELETE of the seed"; fi

# --- the baseline file is never written -------------------------------------------
if cmp -s "${SHIPPED}" "${FAKE_AGENT_FLAGS}"; then ok "the baseline file is unchanged"; else not_ok "the baseline file is unchanged"; fi

expect "unknown action is rejected" 2 "usage:" -- maybe

echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
