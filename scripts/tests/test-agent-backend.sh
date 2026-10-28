#!/usr/bin/env bash
# Self-contained tests for scripts/agent-backend.sh (#1232).
#
# Runs the helper against scripts/tests/fake_flag_service.py serving a fixture
# agent flag set (the decision.* flags of the epic #1225 spec) with the REAL
# registry deciding what is mutable, never a running stack (epic #1407).
#
# Runs as part of `make lint` (needs only bash + python3), or directly:
#   ./scripts/tests/test-agent-backend.sh
#
# UC: UC-044 main A3 A4 A5 Post-F-1
# UC: UC-070 A6
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/../agent-backend.sh"
WORK="$(mktemp -d)"
# shellcheck source=scripts/tests/lib-fake-flag-service.sh
source "${SCRIPT_DIR}/lib-fake-flag-service.sh"
trap 'stop_fake_flag_service; rm -rf "${WORK}"' EXIT
export FAKE_AGENT_FLAGS="${WORK}/agent.json"

# fixture: (re)start the fake over a fresh fixture flag set (no overlays).
fixture() {
  stop_fake_flag_service
  write_fixture
  start_fake_flag_service "${WORK}"
}

write_fixture() {
  cat >"${FAKE_AGENT_FLAGS}" <<'JSON'
{
  "$schema": "https://flagd.dev/schema/v0/flags.json",
  "flags": {
    "enabled": {
      "state": "ENABLED",
      "variants": { "on": true, "off": false },
      "defaultVariant": "off"
    },
    "decision.backend": {
      "state": "ENABLED",
      "variants": { "rules": "rules", "claude": "claude", "openai": "openai", "jev": "jev" },
      "defaultVariant": "rules"
    },
    "decision.act_confidence_threshold": {
      "state": "ENABLED",
      "variants": { "low": 0.5, "default": 0.8, "strict": 0.95 },
      "defaultVariant": "default"
    },
    "decision.jev.act_confidence_threshold": {
      "state": "ENABLED",
      "variants": { "inherit": -1.0, "gate": 0.6, "default": 0.8, "strict": 0.95 },
      "defaultVariant": "gate"
    },
    "decision.capture_content": {
      "state": "ENABLED",
      "variants": { "on": true, "off": false },
      "defaultVariant": "on"
    },
    "decision.claude.model": {
      "state": "ENABLED",
      "variants": {
        "haiku": "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-5-5",
        "opus": "claude-opus-5-5"
      },
      "defaultVariant": "haiku"
    },
    "decision.openai.model": {
      "state": "ENABLED",
      "variants": { "unset": "", "gpt": "gpt-5" },
      "defaultVariant": "unset"
    },
    "decision.openai.reasoning_effort": {
      "state": "ENABLED",
      "variants": { "auto": "auto", "none": "none", "minimal": "minimal", "low": "low", "omit": "omit" },
      "defaultVariant": "auto"
    }
  }
}
JSON
}

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

# effective <flag-key>: the variant the flag service serves now.
effective() {
  curl -sS "${FLAG_SERVICE_URL}/agent/$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["effective_default"])'
}

check_variant() {
  local key="$1" want="$2" got
  got="$(effective "${key}")"
  if [[ "${got}" == "${want}" ]]; then ok "${key} == ${want}"; else not_ok "${key} == ${want} (got ${got})"; fi
}

# --- status ---------------------------------------------------------------
fixture
expect "status (no args) shows backend" 0 'backend   = rules ("rules")' --
expect "status shows model n/a for rules" 0 "model     = n/a (rules backend)" --
expect "status shows threshold" 0 "threshold = default (0.8)" -- status
expect "status shows capture" 0 "capture   = on (true)" -- status
expect "status shows jev threshold" 0 "jev thr.  = gate (0.6)" -- status
expect "status shows the flag service address" 0 "(${FLAG_SERVICE_URL})" -- status
# UC-044 A5: status (with or without the word) only reads; it writes nothing.
if grep -qE '^(PUT|DELETE|POST) ' "${FAKE_FLAG_LOG}"; then
  not_ok "status writes nothing (log: $(grep -E '^(PUT|DELETE|POST) ' "${FAKE_FLAG_LOG}" | tr '\n' ';'))"
elif ! grep -qE '^GET ' "${FAKE_FLAG_LOG}"; then
  not_ok "status writes nothing (the request log saw no GET either; is it recording?)"
else
  ok "status writes nothing (only GETs reached the flag service)"
fi

# --- backend + model ------------------------------------------------------
fixture
expect "flip to claude sonnet" 0 'agent backend -> claude  model -> sonnet ("claude-sonnet-5-5")' -- claude sonnet
check_variant decision.backend claude
check_variant decision.claude.model sonnet
expect "status shows claude model" 0 'model     = sonnet ("claude-sonnet-5-5")' -- status
expect "flip backend only" 0 "agent backend -> openai" -- openai
check_variant decision.backend openai
check_variant decision.claude.model sonnet
expect "back to rules" 0 "agent backend -> rules" -- rules
check_variant decision.backend rules

# --- threshold / capture --------------------------------------------------
fixture
expect "start the tuning beats on claude" 0 "agent backend -> claude" -- claude
expect "threshold strict" 0 "agent act threshold -> strict (0.95)" -- threshold strict
check_variant decision.act_confidence_threshold strict
check_variant decision.jev.act_confidence_threshold gate
expect "jev-threshold inherit" 0 "agent jev act threshold -> inherit (-1.0)" -- jev-threshold inherit
check_variant decision.jev.act_confidence_threshold inherit
check_variant decision.act_confidence_threshold strict
expect "status shows both thresholds" 0 "jev thr.  = inherit (-1.0)" -- status
expect "jev-threshold strict" 0 "agent jev act threshold -> strict (0.95)" -- jev-threshold strict
check_variant decision.jev.act_confidence_threshold strict
expect "capture off" 0 "agent content capture -> off" -- capture off
check_variant decision.capture_content off
expect "effort omit" 0 'agent openai reasoning effort -> omit ("omit")' -- effort omit
check_variant decision.openai.reasoning_effort omit
# UC-044 A3: tuning never switches the backend.
check_variant decision.backend claude
expect "status after effort flip" 0 "capture   = off (false)" -- status
"${HELPER}" openai >/dev/null
expect "status shows effort for openai" 0 'effort    = omit ("omit")' -- status

# --- refusals change nothing ----------------------------------------------
fixture
expect "unknown model lists valid ones" 1 "valid: haiku, sonnet, opus" -- claude gpt4
check_variant decision.backend rules
expect "unknown threshold lists valid ones" 1 "valid: low, default, strict" -- threshold yolo
expect "unknown capture variant refused" 1 "valid: on, off" -- capture maybe
expect "unknown effort lists valid ones" 1 "valid: auto, none, minimal, low, omit" -- effort high
expect "effort needs a value" 2 "usage:" -- effort
expect "rules takes no model" 1 "rules backend has no model" -- rules haiku
expect "missing model flag is named" 1 "flag 'decision.jev.model' not defined" -- jev small
expect "unknown action shows usage" 2 "usage:" -- banana
expect "threshold needs a value" 2 "usage:" -- threshold
expect "unknown jev-threshold lists valid ones" 1 "valid: inherit, gate, default, strict" -- jev-threshold low
expect "jev-threshold needs a value" 2 "usage:" -- jev-threshold
overlays="$(curl -sS "${FLAG_SERVICE_URL}/state" | python3 -c 'import json,sys; print(json.load(sys.stdin)["overlays"])')"
if [[ "${overlays}" == "0" ]]; then
  ok "refused flips leave every flag at its baseline"
else
  not_ok "refused flips leave every flag at its baseline"
fi

# --- writes: writer label; backend and model persisted (C-018, #1682) -----
fixture
"${HELPER}" claude opus >/dev/null
puts="$(grep '^PUT ' "${FAKE_FLAG_LOG}")"
if [[ "$(grep -c . <<<"${puts}")" -eq 2 && "${puts}" == *'"writer": "stage"'* && "$(grep -c '"persist": true' <<<"${puts}")" -eq 2 ]]; then
  ok "a backend+model flip is two persisted PUTs, writer=stage"
else
  not_ok "a backend+model flip is two persisted PUTs, writer=stage (${puts})"
fi
fixture
"${HELPER}" threshold strict >/dev/null
if ! grep '^PUT ' "${FAKE_FLAG_LOG}" | grep -q persist; then
  ok "a threshold flip stays in memory (not persisted)"
else
  not_ok "a threshold flip stays in memory (not persisted)"
fi

# --- a failed write never leaves a half switch (#1680) ---------------------
# The 2nd PUT fails (the backend write, model goes first): the previous backend stays.
fixture
stop_fake_flag_service
start_fake_flag_service "${WORK}" --fail-put 2
expect "failed second write fails the switch" 1 "flag service" -- claude sonnet
check_variant decision.backend rules
# The model was written first (validated) and stays set; it is inert while rules is active.
check_variant decision.claude.model sonnet
# The 1st PUT fails: nothing is written at all.
stop_fake_flag_service
start_fake_flag_service "${WORK}" --fail-put 1
expect "failed first write fails the switch" 1 "flag service" -- claude sonnet
check_variant decision.backend rules
check_variant decision.claude.model haiku
# A backend variant the service does not declare is refused before any write.
fixture
python3 - "${FAKE_AGENT_FLAGS}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
del d["flags"]["decision.backend"]["variants"]["claude"]
json.dump(d, open(sys.argv[1], "w"))
PY
stop_fake_flag_service
start_fake_flag_service "${WORK}"
expect "undeclared backend variant is refused" 1 "variant 'claude' not defined for flag 'decision.backend'" -- claude sonnet
check_variant decision.claude.model haiku

# --- UC-070 A6: second vendor after the rollback to jev ---------------------
# A model variant the flag file does not define is refused before anything is
# switched (jev stays); a defined one switches to openai; `jev` switches back.
fixture
"${HELPER}" jev >/dev/null
expect "A6 undefined openai model variant is refused" 1 "valid: unset, gpt" -- openai llama
check_variant decision.backend jev
check_variant decision.openai.model unset
expect "A6 a defined openai model variant switches" 0 "agent backend -> openai" -- openai gpt
check_variant decision.backend openai
check_variant decision.openai.model gpt
expect "A6 back to jev" 0 "agent backend -> jev" -- jev
check_variant decision.backend jev

# --- the flag service unreachable -----------------------------------------
# A4: a flip attempted while the service is down fails clearly (non-zero exit, clear message).
# The stopped fake cannot log a PUT, so "nothing written" is not checked here; the refusal
# case above (overlays stay 0) carries the "nothing changes" half.
fixture
"${HELPER}" openai >/dev/null
stop_fake_flag_service
expect "unreachable flag service fails a switch clearly" 1 "flag service unreachable" -- claude sonnet
expect "unreachable flag service fails clearly" 1 "flag service unreachable" -- status
echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
