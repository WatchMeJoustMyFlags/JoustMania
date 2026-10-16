#!/usr/bin/env bash
# Self-contained tests for scripts/talk-stage.sh (#1301, UC-070 BR-003, A7, A8).
#
# Runs the helper against temp copies of the shipped CI flag files (agent.json
# and interventions.json), never the real services/flagd/ci/ files.
#
# Runs as part of `make lint` (needs only bash + python3), or directly:
#   ./scripts/tests/test-talk-stage.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HELPER="${SCRIPT_DIR}/../talk-stage.sh"
PREFLIGHT_BASELINE="${SCRIPT_DIR}/../talk-baseline.json"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export AGENT_FLAG_FILE="${WORK}/agent.json"

pass=0
fail=0
ok() { echo "ok   - $1"; pass=$((pass + 1)); }
not_ok() { echo "FAIL - $1"; fail=$((fail + 1)); }

fresh() {
  cp "${REPO_ROOT}/services/flagd/ci/agent.json" "${WORK}/agent.json"
  cp "${REPO_ROOT}/services/flagd/ci/interventions.json" "${WORK}/interventions.json"
}
variant() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["flags"][sys.argv[2]]["defaultVariant"])' "$1" "$2"
}
expect() { # expect <name> <flag> <variant> [file]
  local got
  got="$(variant "${4:-${AGENT_FLAG_FILE}}" "$2")"
  if [[ "${got}" == "$3" ]]; then ok "$1"; else not_ok "$1 (${2}=${got}, want $3)"; fi
}
setv() { # setv <file> <flag> <variant>
  python3 - "$@" <<'PY'
import json, sys
path, key, variant = sys.argv[1:]
data = json.load(open(path))
data["flags"][key]["defaultVariant"] = variant
json.dump(data, open(path, "w"), indent=2)
PY
}

# --- every baseline flag/variant exists in BOTH shipped flag files ----------
for f in services/flagd/ci/agent.json services/flagd/agent.json; do
  missing="$(python3 - "${REPO_ROOT}/${f}" "${PREFLIGHT_BASELINE}" <<'PY'
import json, sys
flags = json.load(open(sys.argv[1]))["flags"]
base = json.load(open(sys.argv[2]))
bad = [f"{k}={v}" for sec in ("checked", "reset_also") for k, v in base[sec].items()
       if v not in flags.get(k, {}).get("variants", {})]
print(" ".join(bad))
PY
)"
  if [[ -z "${missing}" ]]; then ok "baseline variants exist in ${f}"; else not_ok "baseline variants exist in ${f}: ${missing}"; fi
done

# --- reset restores the stage baseline from any drift (A8) ------------------
fresh
setv "${AGENT_FLAG_FILE}" interventions_allowed none
setv "${AGENT_FLAG_FILE}" enabled off
setv "${AGENT_FLAG_FILE}" decision.backend claude
setv "${AGENT_FLAG_FILE}" decision.jev.base_url blackhole
setv "${WORK}/interventions.json" shield_seconds default
inode_before="$(stat -c %i "${AGENT_FLAG_FILE}")"
out="$("${HELPER}" reset)"
expect "reset: interventions_allowed back to standard" interventions_allowed standard
expect "reset: kill switch back on" enabled on
expect "reset: interventions_enabled on" interventions_enabled on
expect "reset: backend back to rules" decision.backend rules
expect "reset: blackhole undone" decision.jev.base_url default
expect "reset: budget stage" llm.max_requests_per_minute stage
expect "reset: interventions.json neutral" shield_seconds none "${WORK}/interventions.json"
[[ "${out}" == *"reset  interventions_allowed: none -> standard"* ]] && ok "reset reports what it changed" || not_ok "reset reports what it changed: ${out}"
[[ "$(stat -c %i "${AGENT_FLAG_FILE}")" == "${inode_before}" ]] && ok "reset writes in place (same inode)" || not_ok "reset writes in place"
out="$("${HELPER}" status)"
[[ "${out}" == "at the stage baseline" ]] && ok "status after reset" || not_ok "status after reset: ${out}"
before="$(cat "${AGENT_FLAG_FILE}")"
"${HELPER}" reset >/dev/null
[[ "$(cat "${AGENT_FLAG_FILE}")" == "${before}" ]] && ok "reset is idempotent" || not_ok "reset is idempotent"

# --- reset keeps the shipped empty targeting placeholders --------------------
fresh
"${HELPER}" reset >/dev/null
if diff -q "${REPO_ROOT}/services/flagd/ci/interventions.json" "${WORK}/interventions.json" >/dev/null; then
  ok "reset leaves the shipped interventions.json byte-identical (targeting {} kept)"
else
  not_ok "reset leaves the shipped interventions.json byte-identical: $(diff "${REPO_ROOT}/services/flagd/ci/interventions.json" "${WORK}/interventions.json" | head -5)"
fi
python3 - "${WORK}/interventions.json" <<'PY2'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["flags"]["shield_seconds"]["targeting"] = {"if": [{"==": [{"var": "targetingKey"}, "MOCK0001"]}, "agent_MOCK0001", None]}
json.dump(data, open(path, "w"), indent=2)
PY2
out="$("${HELPER}" reset)"
[[ "${out}" == *"shield_seconds: targeting removed"* ]] && ok "reset removes agent-written targeting" || not_ok "reset removes agent-written targeting: ${out}"
python3 - "${WORK}/interventions.json" "${REPO_ROOT}/services/flagd/ci/interventions.json" <<'PY2' && ok "other flags keep their empty targeting placeholder" || not_ok "other flags keep their empty targeting placeholder"
import json, sys
now, shipped = (json.load(open(p))["flags"] for p in sys.argv[1:])
kept = [k for k, f in shipped.items() if f.get("targeting") == {} and k != "shield_seconds"]
sys.exit(0 if kept and all(now[k].get("targeting") == {} for k in kept) else 1)
PY2

# --- the reset baseline passes the pre-flight's flag section ---------------
# docker and curl stubbed to "unavailable": only the flag-file section runs, and
# no real container is ever inspected or exec'd.
mkdir -p "${WORK}/bin"
printf '#!/usr/bin/env bash\nexit 1\n' >"${WORK}/bin/docker"
printf '#!/usr/bin/env bash\nexit 1\n' >"${WORK}/bin/curl"
chmod +x "${WORK}/bin/docker" "${WORK}/bin/curl"
TALK_CLOCK_URLS="" PATH="${WORK}/bin:${PATH}" "${SCRIPT_DIR}/../talk-preflight.sh" >"${WORK}/pf.out" 2>&1 || true
if grep -q "stage baseline is\|nothing can act\|allows nothing" "${WORK}/pf.out" \
  || ! grep -q "PASS  perceptible interventions allowed" "${WORK}/pf.out"; then
  not_ok "pre-flight flag checks pass after reset: $(grep FAIL "${WORK}/pf.out" | head -3)"
else
  ok "pre-flight flag checks pass after reset"
fi

# --- permissions beat restores on normal end (A7) --------------------------
fresh
"${HELPER}" reset >/dev/null
"${HELPER}" permissions none 1 >/dev/null
expect "permissions: restored after the hold" interventions_allowed standard

# --- permissions beat restores when interrupted (TERM, HUP) -----------------
for sig in TERM HUP; do
  fresh
  "${HELPER}" reset >/dev/null
  "${HELPER}" permissions none 30 >/dev/null 2>&1 &
  pid=$!
  for _ in $(seq 50); do
    [[ "$(variant "${AGENT_FLAG_FILE}" interventions_allowed)" == "none" ]] && break
    sleep 0.1
  done
  expect "permissions (${sig}): held at none during the beat" interventions_allowed none
  kill "-${sig}" "${pid}"
  wait "${pid}" 2>/dev/null || true
  expect "permissions (${sig}): restored after the interruption" interventions_allowed standard
done

# --- unknown variant is refused and leaves the baseline ---------------------
fresh
"${HELPER}" reset >/dev/null
if "${HELPER}" permissions nonsense 1 >/dev/null 2>&1; then not_ok "unknown variant refused"; else ok "unknown variant refused"; fi
expect "unknown variant leaves the baseline" interventions_allowed standard

# --- usage -----------------------------------------------------------------
rc=0; "${HELPER}" >/dev/null 2>&1 || rc=$?
[[ "${rc}" -eq 2 ]] && ok "no action is a usage error" || not_ok "no action is a usage error (rc=${rc})"
rc=0; "${HELPER}" permissions none soon >/dev/null 2>&1 || rc=$?
[[ "${rc}" -eq 2 ]] && ok "bad seconds is a usage error" || not_ok "bad seconds is a usage error (rc=${rc})"

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
