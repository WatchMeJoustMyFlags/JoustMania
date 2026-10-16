#!/usr/bin/env bash
# Self-contained tests for scripts/agent-killswitch.sh (UC-045 main steps 1 and 7).
#
# Runs the helper against a temp copy of services/flagd/ci/agent.json
# (AGENT_FLAG_FILE override), never the real file.
#
# Runs as part of `make lint` (needs only bash + python3), or directly:
#   ./scripts/tests/test-agent-killswitch.sh
#
# UC: UC-045 main
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HELPER="${SCRIPT_DIR}/../agent-killswitch.sh"
SHIPPED="${REPO_ROOT}/services/flagd/ci/agent.json"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export AGENT_FLAG_FILE="${WORK}/agent.json"

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

enabled_variant() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["flags"]["enabled"]["defaultVariant"])' "${AGENT_FLAG_FILE}"
}

# Every flag other than `enabled` is semantically unchanged versus the shipped file.
others_unchanged() {
  python3 - "${SHIPPED}" "${AGENT_FLAG_FILE}" <<'PY'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
fa, fb = a["flags"], b["flags"]
assert fa.keys() == fb.keys(), "flag keys changed"
for key in fa:
    if key != "enabled":
        assert fa[key] == fb[key], f"{key} changed"
assert fa["enabled"]["variants"] == fb["enabled"]["variants"], "enabled variants changed"
assert {k: v for k, v in a.items() if k != "flags"} == {k: v for k, v in b.items() if k != "flags"}, "top-level keys changed"
PY
}

cp "${SHIPPED}" "${AGENT_FLAG_FILE}"
expect "status (no args) shows the current value" 0 "agent kill-switch = $(enabled_variant)" --

# --- step 1: off --------------------------------------------------------------
expect "off sets enabled off" 0 "agent kill-switch -> off" -- off
if [[ "$(enabled_variant)" == "off" ]]; then ok "enabled == off"; else not_ok "enabled == off (got $(enabled_variant))"; fi
if others_unchanged; then ok "off leaves every other flag unchanged"; else not_ok "off leaves every other flag unchanged"; fi
expect "status shows off" 0 "agent kill-switch = off" -- status

# --- step 7: on ---------------------------------------------------------------
expect "on sets enabled on" 0 "agent kill-switch -> on" -- on
if [[ "$(enabled_variant)" == "on" ]]; then ok "enabled == on"; else not_ok "enabled == on (got $(enabled_variant))"; fi
if others_unchanged; then ok "on leaves every other flag unchanged"; else not_ok "on leaves every other flag unchanged"; fi
if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "${AGENT_FLAG_FILE}" && [[ "$(tail -c1 "${AGENT_FLAG_FILE}" | od -An -c | tr -d ' ')" == '\n' ]]; then
  ok "flag file stays valid JSON with a trailing newline"
else
  not_ok "flag file stays valid JSON with a trailing newline"
fi

# --- rejections leave the file unchanged --------------------------------------
cp "${AGENT_FLAG_FILE}" "${WORK}/before.json"
expect "unknown action is rejected" 2 "usage:" -- maybe
if cmp -s "${AGENT_FLAG_FILE}" "${WORK}/before.json"; then ok "rejected action leaves the file unchanged"; else not_ok "rejected action leaves the file unchanged"; fi

python3 - "${AGENT_FLAG_FILE}" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["flags"]["enabled"]["variants"] = {"on": True}
json.dump(data, open(path, "w"), indent=2)
PY
cp "${AGENT_FLAG_FILE}" "${WORK}/before.json"
expect "a variant missing from the file is refused" 1 "variant 'off' not defined" -- off
if cmp -s "${AGENT_FLAG_FILE}" "${WORK}/before.json"; then ok "refused flip leaves the file unchanged"; else not_ok "refused flip leaves the file unchanged"; fi

echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
