#!/usr/bin/env bash
# Self-contained tests for scripts/talk-stage.sh (#1301, #1316, UC-070 BR-001, BR-003, BR-007, A7, A8, A9; UC-053 A1).
#
# Runs the helper against scripts/tests/fake_flag_service.py serving the shipped
# CI agent flag set with the REAL registry (epic #1407), never a running stack.
#
# Runs as part of `make lint` (needs only bash + python3), or directly:
#   ./scripts/tests/test-talk-stage.sh
#
# UC: UC-070 main A7 A8 A9 BR-001 BR-003 BR-007 Pre-3 Post-S-2
# UC: UC-053 A1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HELPER="${SCRIPT_DIR}/../talk-stage.sh"
PREFLIGHT_BASELINE="${SCRIPT_DIR}/../talk-baseline.json"
WORK="$(mktemp -d)"
# shellcheck source=scripts/tests/lib-fake-flag-service.sh
source "${SCRIPT_DIR}/lib-fake-flag-service.sh"
trap 'stop_fake_flag_service; rm -rf "${WORK}"' EXIT

pass=0
fail=0
# BR-001: the helper changes flags only through the flag service; the shipped
# flag files (rehearsal and stage baselines) are never edited.
flag_files_sum() { cat "${REPO_ROOT}"/services/flagd/*.json "${REPO_ROOT}"/services/flagd/ci/*.json | cksum; }
FLAG_FILES_BEFORE="$(flag_files_sum)"
ok() { echo "ok   - $1"; pass=$((pass + 1)); }
not_ok() { echo "FAIL - $1"; fail=$((fail + 1)); }

fresh() { # a flag service at the shipped baseline, no overlays
  stop_fake_flag_service
  start_fake_flag_service "${WORK}"
}
variant() { # variant <flag>: what the flag service serves now
  curl -sS "${FLAG_SERVICE_URL}/agent/$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["effective_default"])'
}
expect() { # expect <name> <flag> <variant>
  local got
  got="$(variant "$2")"
  if [[ "${got}" == "$3" ]]; then ok "$1"; else not_ok "$1 (${2}=${got}, want $3)"; fi
}
drift() { # drift <flag> <variant>: another writer moves a flag (a beat, an operator)
  curl -sS -o /dev/null -X PUT -H 'Content-Type: application/json' \
    --data "{\"variant\": \"$2\", \"writer\": \"operator\"}" "${FLAG_SERVICE_URL}/agent/$1"
}
snapshot() { # every flag's effective variant, one per line
  curl -sS "${FLAG_SERVICE_URL}/agent" | python3 -c 'import json,sys; [print(f["flag"], f["effective_default"]) for f in json.load(sys.stdin)["flags"]]'
}

# --- every baseline flag/variant exists in BOTH shipped flag files and is mutable
for f in services/flagd/ci/agent.json services/flagd/agent.json; do
  missing="$(python3 - "${REPO_ROOT}/${f}" "${PREFLIGHT_BASELINE}" "${REPO_ROOT}/services/flag-service/registry.yaml" <<'PY'
import json, re, sys
flags = json.load(open(sys.argv[1]))["flags"]
base = json.load(open(sys.argv[2]))
registry = set(re.findall(r"\{set: agent, key: ([\w.-]+),", open(sys.argv[3]).read()))
bad = [f"{k}={v}" for sec in ("checked", "reset_also", "fallback") for k, v in base[sec].items()
       if v not in flags.get(k, {}).get("variants", {})]
bad += [f"{k} (not in registry.yaml)" for sec in ("checked", "reset_also", "fallback") for k in base[sec]
        if k not in registry]
print(" ".join(sorted(set(bad))))
PY
)"
  if [[ -z "${missing}" ]]; then ok "baseline variants exist in ${f} and are mutable"; else not_ok "baseline variants exist in ${f}: ${missing}"; fi
done

# --- reset restores the stage baseline from any drift (A8) ------------------
fresh
drift interventions_allowed none
drift enabled off
drift decision.backend claude
drift decision.jev.base_url blackhole
out="$("${HELPER}" reset)"
expect "reset: interventions_allowed back to standard" interventions_allowed standard
expect "reset: kill switch back on" enabled on
expect "reset: interventions_enabled on" interventions_enabled on
expect "reset: backend back to rules" decision.backend rules
expect "reset: blackhole undone" decision.jev.base_url default
expect "reset: budget stage" llm.max_requests_per_minute stage
[[ "${out}" == *"reset  interventions_allowed: none -> standard"* ]] && ok "reset reports what it changed" || not_ok "reset reports what it changed: ${out}"
[[ "${out}" == *"interventions: 0 game-scoped value(s) in 0 live game(s)"* ]] && ok "reset reports interventions as per game" || not_ok "reset reports interventions as per game: ${out}"
if grep -q '^DELETE /agent?writer=stage' "${FAKE_FLAG_LOG}"; then ok "reset clears the stage overlays first"; else not_ok "reset clears the stage overlays first"; fi
if grep -q '"persist"' "${FAKE_FLAG_LOG}"; then not_ok "the stage never persists (N4)"; else ok "the stage never persists (N4)"; fi
out="$("${HELPER}" status)"
[[ "${out}" == "at the stage baseline" ]] && ok "status after reset" || not_ok "status after reset: ${out}"
before="$(snapshot)"
"${HELPER}" reset >/dev/null
[[ "$(snapshot)" == "${before}" ]] && ok "reset is idempotent" || not_ok "reset is idempotent"

# --- reset clears the persisted backend and model (#1682, C-018) -----------
# agent-backend.sh persists decision.backend and the model: a stage overlay over
# them would not survive a flag-service restart, so reset deletes them; the
# host's other persisted settings (the menu's interventions_allowed) stay.
persisted() { # persisted <flag>: the persisted admin variant, or "none"
  curl -sS "${FLAG_SERVICE_URL}/agent/$1" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("admin_value") or {}).get("variant", "none"))'
}
persist() { # persist <flag> <variant>: an operator's persisted write
  curl -sS -o /dev/null -X PUT -H 'Content-Type: application/json' \
    --data "{\"variant\": \"$2\", \"writer\": \"stage\", \"persist\": true}" "${FLAG_SERVICE_URL}/agent/$1"
}
fresh
persist decision.backend jev
persist decision.claude.model opus
persist interventions_allowed ambient
out="$("${HELPER}" reset)"
[[ "$(persisted decision.backend)" == "none" ]] && ok "reset clears the persisted backend" || not_ok "reset clears the persisted backend ($(persisted decision.backend))"
[[ "$(persisted decision.claude.model)" == "none" ]] && ok "reset clears the persisted model" || not_ok "reset clears the persisted model ($(persisted decision.claude.model))"
[[ "$(persisted interventions_allowed)" == "ambient" ]] && ok "reset keeps the host's other persisted settings" || not_ok "reset keeps the host's other persisted settings ($(persisted interventions_allowed))"
expect "reset after a persisted jev: backend rules" decision.backend rules
[[ "${out}" == *"reset  decision.backend: persisted jev cleared"* ]] && ok "reset reports the cleared persisted backend" || not_ok "reset reports the cleared persisted backend: ${out}"
if grep -q '^DELETE /agent/decision.backend?persist=true' "${FAKE_FLAG_LOG}"; then ok "reset deletes the persisted backend (persist=true)"; else not_ok "reset deletes the persisted backend (persist=true)"; fi

# --- the reset baseline passes the pre-flight's flag section ---------------
# docker and curl stubbed to "unavailable": only the flag section runs (it reads
# the flag service in Python), and no real container is inspected.
mkdir -p "${WORK}/bin"
printf '#!/usr/bin/env bash\nexit 1\n' >"${WORK}/bin/docker"
REAL_CURL="$(command -v curl)"
# curl "unavailable" except for the flag service (flagservice.py uses curl, #2057)
cat >"${WORK}/bin/curl" <<SH
#!/usr/bin/env bash
case "\${@: -1}" in "${FLAG_SERVICE_URL}"*) exec "${REAL_CURL}" "\$@" ;; esac
exit 1
SH
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
    [[ "$(variant interventions_allowed)" == "none" ]] && break
    sleep 0.1
  done
  expect "permissions (${sig}): held at none during the beat" interventions_allowed none
  kill "-${sig}" "${pid}"
  wait "${pid}" 2>/dev/null || true
  expect "permissions (${sig}): restored after the interruption" interventions_allowed standard
done

# --- permissions beat restores on Ctrl-C (INT) ------------------------------
# A background job of a non-interactive shell starts with SIGINT ignored, and an
# ignored signal cannot be trapped; job control (set -m) starts it with the
# default disposition, as a presenter's foreground helper has.
fresh
"${HELPER}" reset >/dev/null
set -m
"${HELPER}" permissions none 30 >/dev/null 2>&1 &
pid=$!
set +m
for _ in $(seq 50); do
  [[ "$(variant interventions_allowed)" == "none" ]] && break
  sleep 0.1
done
expect "permissions (INT): held at none during the beat" interventions_allowed none
kill -INT "${pid}"
# Poll instead of `wait`: a helper that ignored INT would only restore after its
# 30 s hold, which must not count as restored on Ctrl-C.
for _ in $(seq 30); do
  [[ "$(variant interventions_allowed)" == "standard" ]] && break
  sleep 0.1
done
expect "permissions (INT): restored at once on Ctrl-C" interventions_allowed standard
kill -TERM "${pid}" 2>/dev/null || true
wait "${pid}" 2>/dev/null || true

# --- unknown variant is refused and leaves the baseline ---------------------
fresh
"${HELPER}" reset >/dev/null
if "${HELPER}" permissions nonsense 1 >/dev/null 2>&1; then not_ok "unknown variant refused"; else ok "unknown variant refused"; fi
expect "unknown variant leaves the baseline" interventions_allowed standard

# --- fallback on / off / reset (UC-070 A9, BR-007, #1316) --------------------
fresh
"${HELPER}" reset >/dev/null
out="$("${HELPER}" fallback on)"
expect "fallback on: shadow games eligible for remote backends" llm.eligible_game_kinds real_and_shadow
expect "fallback on: experiment loop on" experiments_enabled on
expect "fallback on: the agent declares experiments" experiment_dynamic_enabled on
expect "fallback on: fast experiment tick" experiment_tick_seconds fast
expect "fallback on: other baseline flags untouched" interventions_allowed standard
[[ "${out}" == *"set    llm.eligible_game_kinds: real_only -> real_and_shadow"* ]] && ok "fallback on reports what it changed" || not_ok "fallback on reports what it changed: ${out}"
[[ "${out}" != *"WARN"* ]] && ok "fallback on: shipped selection includes calm_long (no warning)" || not_ok "fallback on: no calm_long warning: ${out}"
out="$("${HELPER}" status)"
[[ "${out}" == *"fallback: on"* ]] && ok "status names the fallback" || not_ok "status names the fallback: ${out}"
before="$(snapshot)"
"${HELPER}" fallback on >/dev/null
[[ "$(snapshot)" == "${before}" ]] && ok "fallback on is idempotent" || not_ok "fallback on is idempotent"
"${HELPER}" fallback off >/dev/null
expect "fallback off: real games only" llm.eligible_game_kinds real_only
expect "fallback off: experiment loop off" experiments_enabled off
expect "fallback off: dynamic declarer off" experiment_dynamic_enabled off
expect "fallback off: default experiment tick" experiment_tick_seconds default
out="$("${HELPER}" status)"
[[ "${out}" == "at the stage baseline" ]] && ok "status after fallback off" || not_ok "status after fallback off: ${out}"
"${HELPER}" fallback on >/dev/null
drift experiment.mock_archetypes quick_cascade
"${HELPER}" reset >/dev/null
expect "reset after fallback on: real games only" llm.eligible_game_kinds real_only
expect "reset after fallback on: experiment loop off" experiments_enabled off
expect "reset after fallback on: dynamic declarer off" experiment_dynamic_enabled off
expect "reset after fallback on: default experiment tick" experiment_tick_seconds default
expect "reset restores the archetype selection with calm_long" experiment.mock_archetypes all
drift experiment.mock_archetypes quick_cascade
out="$("${HELPER}" fallback on)"
[[ "${out}" == *"WARN   experiment.mock_archetypes = quick_cascade"*"no calm_long"* ]] && ok "fallback on warns without calm_long" || not_ok "fallback on warns without calm_long: ${out}"
rc=0; "${HELPER}" fallback maybe >/dev/null 2>&1 || rc=$?
[[ "${rc}" -eq 2 ]] && ok "fallback needs on|off" || not_ok "fallback needs on|off (rc=${rc})"
rc=0; "${HELPER}" fallback >/dev/null 2>&1 || rc=$?
[[ "${rc}" -eq 2 ]] && ok "fallback without state is a usage error" || not_ok "fallback without state is a usage error (rc=${rc})"

# --- the flag service unreachable -------------------------------------------
stop_fake_flag_service
rc=0; out="$("${HELPER}" status 2>&1)" || rc=$?
[[ "${rc}" -ne 0 && "${out}" == *"flag service unreachable"* ]] && ok "unreachable flag service fails clearly" || not_ok "unreachable flag service fails clearly (rc=${rc}: ${out})"

# --- usage -----------------------------------------------------------------
rc=0; "${HELPER}" >/dev/null 2>&1 || rc=$?
[[ "${rc}" -eq 2 ]] && ok "no action is a usage error" || not_ok "no action is a usage error (rc=${rc})"
rc=0; "${HELPER}" permissions none soon >/dev/null 2>&1 || rc=$?
[[ "${rc}" -eq 2 ]] && ok "bad seconds is a usage error" || not_ok "bad seconds is a usage error (rc=${rc})"

# --- BR-001: no flag file was edited by any beat ---------------------------
if [[ "$(flag_files_sum)" == "${FLAG_FILES_BEFORE}" ]]; then
  ok "BR-001: the shipped flag files are unchanged (flags change only through the flag service)"
else
  not_ok "BR-001: a beat edited a shipped flag file under services/flagd/"
fi

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
