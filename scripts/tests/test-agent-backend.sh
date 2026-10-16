#!/usr/bin/env bash
# Self-contained tests for scripts/agent-backend.sh (#1232).
#
# Runs the helper against a temp copy of a flag file holding the decision.*
# flags from the epic #1225 spec (AGENT_FLAG_FILE override), never the real
# services/flagd/ci/agent.json.
#
# Usage: ./scripts/tests/test-agent-backend.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/../agent-backend.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export AGENT_FLAG_FILE="${WORK}/agent.json"

fixture() {
  cat >"${AGENT_FLAG_FILE}" <<'JSON'
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
    }
  }
}
JSON
  chmod 664 "${AGENT_FLAG_FILE}"
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

# default_variant <flag-key>
default_variant() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["flags"][sys.argv[2]]["defaultVariant"])' \
    "${AGENT_FLAG_FILE}" "$1"
}

check_variant() {
  local key="$1" want="$2" got
  got="$(default_variant "${key}")"
  if [[ "${got}" == "${want}" ]]; then ok "${key} == ${want}"; else not_ok "${key} == ${want} (got ${got})"; fi
}

# --- status ---------------------------------------------------------------
fixture
expect "status (no args) shows backend" 0 'backend   = rules ("rules")' --
expect "status shows model n/a for rules" 0 "model     = n/a (rules backend)" --
expect "status shows threshold" 0 "threshold = default (0.8)" -- status
expect "status shows capture" 0 "capture   = on (true)" -- status

# --- backend + model ------------------------------------------------------
fixture
expect "flip to claude sonnet" 0 'agent backend -> claude  model -> sonnet ("claude-sonnet-5-5")' -- claude sonnet
check_variant decision.backend claude
check_variant decision.claude.model sonnet
expect "status shows claude model" 0 'model     = sonnet ("claude-sonnet-5-5")' -- status
expect "flip backend only" 0 "agent backend -> openai  (" -- openai
check_variant decision.backend openai
check_variant decision.claude.model sonnet
expect "back to rules" 0 "agent backend -> rules" -- rules
check_variant decision.backend rules

# --- threshold / capture --------------------------------------------------
fixture
expect "threshold strict" 0 "agent act threshold -> strict (0.95)" -- threshold strict
check_variant decision.act_confidence_threshold strict
expect "capture off" 0 "agent content capture -> off" -- capture off
check_variant decision.capture_content off

# --- refusals leave the file untouched ------------------------------------
fixture
before="$(cat "${AGENT_FLAG_FILE}")"
expect "unknown model lists valid ones" 1 "valid: haiku, sonnet, opus" -- claude gpt4
expect "unknown threshold lists valid ones" 1 "valid: low, default, strict" -- threshold yolo
expect "unknown capture variant refused" 1 "valid: on, off" -- capture maybe
expect "rules takes no model" 1 "rules backend has no model" -- rules haiku
expect "missing model flag is named" 1 "flag 'decision.jev.model' not defined" -- jev small
expect "unknown action shows usage" 2 "usage:" -- banana
expect "threshold needs a value" 2 "usage:" -- threshold
if [[ "$(cat "${AGENT_FLAG_FILE}")" == "${before}" ]]; then
  ok "refused flips leave the file untouched"
else
  not_ok "refused flips leave the file untouched"
fi

# --- atomic write keeps mode, leaves no temp files, only defaultVariant changes
fixture
"${HELPER}" claude opus >/dev/null
mode="$(stat -c %a "${AGENT_FLAG_FILE}")"
if [[ "${mode}" == "664" ]]; then ok "file mode preserved"; else not_ok "file mode preserved (got ${mode})"; fi
if compgen -G "${WORK}/.agent-backend-*" >/dev/null; then not_ok "no temp files left"; else ok "no temp files left"; fi
cp "${AGENT_FLAG_FILE}" "${WORK}/after.json"
fixture
diff_out="$(python3 - "${AGENT_FLAG_FILE}" "${WORK}/after.json" <<'PY'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:])
for key in a["flags"]:
    fa, fb = a["flags"][key], b["flags"][key]
    for field in set(fa) | set(fb):
        if fa.get(field) != fb.get(field):
            print(f"{key}.{field}")
PY
)"
want=$'decision.backend.defaultVariant\ndecision.claude.model.defaultVariant'
if [[ "$(sort <<<"${diff_out}")" == "${want}" ]]; then
  ok "only defaultVariant fields changed"
else
  not_ok "only defaultVariant fields changed (diff: ${diff_out})"
fi

# --- missing decision.* flags (pre-#1226 file) ----------------------------
cat >"${AGENT_FLAG_FILE}" <<'JSON'
{ "flags": { "enabled": { "state": "ENABLED", "variants": { "on": true, "off": false }, "defaultVariant": "off" } } }
JSON
expect "status flags missing keys" 1 "backend   = MISSING (flag not in file)" -- status
expect "flip with missing backend flag fails clearly" 1 "flag 'decision.backend' not defined" -- claude
rm -f "${AGENT_FLAG_FILE}"
expect "missing flag file fails clearly" 1 "flag file not found" -- status
echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
