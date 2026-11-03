#!/usr/bin/env bash
# Self-contained tests for scripts/agent-group-ab.sh (#2467 PR 6): the real-game guard (start, between
# games and polled during a game), snapshot and restore of every flag it writes (writer and TTL kept,
# an operator change mid-run left alone), SIGTERM and SIGKILL + `restore --out`, the A,B,B,A order, the
# first-game verification and the report's decide.prompt_profile filter. #2530: the prompt profile is
# written once per arm (experiment+arm, never stack), every game is bound to its arm, a record without
# the binding stops the run, eligible kinds is shadow_only, and every exit clears the arm values.
# Fake flag service + stubbed docker, no stack.
#
# UC: UC-040 A47 BR-033 BR-036
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AB="${SCRIPT_DIR}/../agent-group-ab.sh"
FLAGS="${SCRIPT_DIR}/../flags.sh"
TOOL="${SCRIPT_DIR}/../agent_group_ab.py"
WORK="$(mktemp -d)"
# shellcheck source=scripts/tests/lib-fake-flag-service.sh
source "${SCRIPT_DIR}/lib-fake-flag-service.sh"
trap 'stop_fake_flag_service; rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
echo '[]' >"${WORK}/games.json"
start_fake_flag_service "${WORK}" --games-file "${WORK}/games.json"
fails=0
ok() { # ok <name> <exit code of the condition>
  if [[ "$2" == 0 ]]; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi
}
# ov <flag>: "spec<TAB>writer<TAB>expires" of the stack overlay.
ov() { python3 "${TOOL}" overlay <<<"$("${FLAGS}" get agent "$1")"; }
ov_spec() { ov "$1" | cut -f1; }
ov_writer() { ov "$1" | cut -f2; }
ov_has_ttl() { [[ "$(ov "$1" | cut -f3)" != "-" ]]; }
TOUCHED=(decision.prompt_profile decision.backend interventions_allowed llm.eligible_game_kinds
  experiment_dynamic_enabled policy.group_grant.enabled)
# arms_json: the arm values of decision.prompt_profile as {"<exp>/<arm>": "<variant>"}.
arms_json() {
  "${FLAGS}" get agent decision.prompt_profile | python3 -c '
import json, sys
v = json.load(sys.stdin).get("experiments") or {}
print(json.dumps({e + "/" + a: x["variant"] for e, arms in v.items() for a, x in arms.items()}, sort_keys=True))'
}
# arms_written: both arm values of one experiment are set (A v1, B neutral): the run has started.
arms_written() {
  arms_json | python3 -c '
import json, sys
v = json.load(sys.stdin)
exps = {k.split("/")[0] for k in v}
sys.exit(0 if len(exps) == 1 and sorted(v.values()) == ["neutral", "v1"] else 1)'
}
no_arms() { [[ "$(arms_json)" == "{}" ]]; }
clear_all() {
  local f
  for f in "${TOUCHED[@]}" policy.group_grant.real_games; do
    "${FLAGS}" clear agent "${f}" >/dev/null
    "${FLAGS}" clear agent "${f}" --persist >/dev/null 2>&1 || true
  done
}
wait_until() { # wait_until <seconds> <command...>
  local n=$(($1 * 10)) i
  shift
  for ((i = 0; i < n; i++)); do "$@" && return 0; sleep 0.1; done
  return 1
}
seed_operator_overlays() {
  "${FLAGS}" set agent interventions_allowed ambient --writer operator --ttl 3600 >/dev/null
  "${FLAGS}" set agent decision.backend claude --writer operator >/dev/null
  "${FLAGS}" set agent experiment_dynamic_enabled on --writer operator >/dev/null
  "${FLAGS}" set agent llm.eligible_game_kinds real_only --writer operator --ttl 3600 >/dev/null
}
operator_overlays_intact() {
  [[ "$(ov_spec interventions_allowed)" == variant:ambient && "$(ov_writer interventions_allowed)" == operator ]] &&
    ov_has_ttl interventions_allowed &&
    [[ "$(ov_spec decision.backend)" == variant:claude && "$(ov_writer decision.backend)" == operator ]] &&
    ! ov_has_ttl decision.backend &&
    [[ "$(ov_spec experiment_dynamic_enabled)" == variant:on && "$(ov_writer experiment_dynamic_enabled)" == operator ]] &&
    [[ "$(ov_spec llm.eligible_game_kinds)" == variant:real_only ]] && ov_has_ttl llm.eligible_game_kinds &&
    [[ "$(ov_spec decision.prompt_profile)" == none && "$(ov_spec policy.group_grant.enabled)" == none ]]
}
all_clear() {
  local f
  for f in "${TOUCHED[@]}"; do
    [[ "$(ov_spec "${f}")" == none ]] || return 1
  done
}
AB_RUN=("${AB}" run --archetype slow_cascade --seeds 1 --reps 1 --no-verify)
run_ab() { "${AB_RUN[@]}" "$@"; }

# Stub docker: `compose ps` answers "running", `ps` lists a leftover container, `compose run` keeps the
# container's SHADOW_GAME_EXPERIMENT_ID / SHADOW_GAME_ARM, `logs` prints a replay record bound to them
# (unbound when the "old-agent" file exists, as an agent image older than #2530) unless the "hang" file
# exists (then it waits a moment and prints nothing). Every call is logged.
cat >"${WORK}/bin/docker" <<SH
#!/usr/bin/env bash
echo "\$*" >>"${WORK}/docker.log"
case "\$1" in
  compose)
    [[ "\$2" == ps ]] && echo abc123
    if [[ "\$2" == run ]]; then
      name="" exp="" arm=""
      while ((\$#)); do
        case "\$1" in
          --name) name="\$2"; shift ;;
          SHADOW_GAME_EXPERIMENT_ID=*) exp="\${1#*=}" ;;
          SHADOW_GAME_ARM=*) arm="\${1#*=}" ;;
        esac
        shift
      done
      printf '%s %s\n' "\${exp}" "\${arm}" >"${WORK}/bind-\${name}"
    fi ;;
  ps) echo cid1 ;;
  logs) if [[ -e "${WORK}/hang" ]]; then sleep 0.5; exit 0; fi
        if [[ -e "${WORK}/flip-on-b" && "\$2" == *-B-* ]]; then echo '[{"game_id":"r","game_kind":"real"}]' >"${WORK}/games.json"; fi
        read -r exp arm <"${WORK}/bind-\$2" || true
        if [[ -e "${WORK}/old-agent" ]]; then
          echo 'PATTERN_REPLAY_RECORD {"game_id":"gfix","outcome":"winner","winner_slot":1}'
        else
          echo "PATTERN_REPLAY_RECORD {\"game_id\":\"gfix\",\"experiment_id\":\"\${exp}\",\"arm\":\"\${arm}\",\"outcome\":\"winner\",\"winner_slot\":1}"
        fi ;;
esac
exit 0
SH
chmod +x "${WORK}/bin/docker"
export PATH="${WORK}/bin:${PATH}"
export PATTERN_REPLAY_TIMEOUT=60

# 1. refusals: a live real game, real_games on, a persisted kill switch. Nothing is touched.
echo '[{"game_id":"g1","game_kind":"real"}]' >"${WORK}/games.json"
code=0
run_ab --out "${WORK}/o1" 2>/dev/null || code=$?
ok "refuses while a real game is live (exit 4)" "$([[ ${code} == 4 ]] && echo 0 || echo 1)"
echo '[{"game_id":"g2","game_kind":"shadow"}]' >"${WORK}/games.json"
"${FLAGS}" set agent policy.group_grant.real_games on --writer operator >/dev/null
code=0
run_ab --out "${WORK}/o1" 2>/dev/null || code=$?
ok "refuses while real_games is on (exit 4)" "$([[ ${code} == 4 ]] && echo 0 || echo 1)"
"${FLAGS}" clear agent policy.group_grant.real_games >/dev/null
"${FLAGS}" set agent interventions_allowed none --persist --writer menu >/dev/null
code=0
run_ab --out "${WORK}/o1" 2>/dev/null || code=$?
ok "refuses to lift a persisted kill switch (exit 4)" "$([[ ${code} == 4 ]] && echo 0 || echo 1)"
"${FLAGS}" clear agent interventions_allowed --persist >/dev/null
ok "refusals changed no flag and never touched docker" "$(all_clear && [[ ! -e "${WORK}/docker.log" ]] && echo 0 || echo 1)"

# 2. a full run over pre-existing operator overlays (writer, TTL): A,B,B,A, everything back exactly.
seed_operator_overlays
"${AB}" run --archetype slow_cascade --seeds 7 --reps 2 --backend openai --no-verify --out "${WORK}/o2" >"${WORK}/out2"
order="$(python3 -c 'import json,sys; print("".join(json.loads(l)["arm"] for l in open(sys.argv[1])))' "${WORK}/o2/manifest.jsonl")"
ok "games run A,B,B,A" "$([[ "${order}" == ABBA ]] && echo 0 || echo 1)"
ok "operator overlays (writer, TTL) come back exactly, experiment_dynamic_enabled included" "$(operator_overlays_intact && echo 0 || echo 1)"
ok "the snapshot is kept as .restored" "$([[ -e "${WORK}/o2/snapshot.tsv.restored" && ! -e "${WORK}/o2/snapshot.tsv" ]] && echo 0 || echo 1)"
ok "agent stopped then started" "$(grep -q 'compose stop agent' "${WORK}/docker.log" && grep -q 'compose start agent' "${WORK}/docker.log" && echo 0 || echo 1)"
ok "group lever on and shadow_group set during the run" "$(grep -q 'PUT.*policy.group_grant.enabled' "${FAKE_FLAG_LOG}" && grep -q 'shadow_group' "${FAKE_FLAG_LOG}" && echo 0 || echo 1)"
ok "experiment_dynamic_enabled was written off during the run" "$(grep 'PUT.*experiment_dynamic_enabled' "${FAKE_FLAG_LOG}" | grep -q '"variant": "off"' && echo 0 || echo 1)"
ok "real_games never written by the run (only the test's own write)" "$([[ "$(grep -c 'PUT.*real_games' "${FAKE_FLAG_LOG}")" == 1 ]] && echo 0 || echo 1)"
ok "seed passed to the one-shot runner" "$(grep -q 'SHADOW_GAME_SEED=7' "${WORK}/docker.log" && echo 0 || echo 1)"
exp2="$(cat "${WORK}/o2/experiment.cleared" 2>/dev/null || true)"
ok "the run's experiment id is kept (group-ab-<epoch>-<pid>)" "$([[ "${exp2}" =~ ^group-ab-[0-9]+-[0-9]+$ ]] && echo 0 || echo 1)"
ok "decision.prompt_profile is never written at stack scope" "$(grep 'PUT /agent/decision.prompt_profile' "${FAKE_FLAG_LOG}" | grep -vq '"scope"' && echo 1 || echo 0)"
ok "the two arm values are written once each (A v1, B neutral)" "$([[ "$(grep -c 'PUT /agent/decision.prompt_profile' "${FAKE_FLAG_LOG}")" == 2 ]] &&
  grep 'PUT /agent/decision.prompt_profile' "${FAKE_FLAG_LOG}" | grep -q "\"variant\": \"v1\", \"scope\": {\"experiment_id\": \"${exp2}\", \"arm\": \"A\"}" &&
  grep 'PUT /agent/decision.prompt_profile' "${FAKE_FLAG_LOG}" | grep -q "\"variant\": \"neutral\", \"scope\": {\"experiment_id\": \"${exp2}\", \"arm\": \"B\"}" && echo 0 || echo 1)"
ok "both arm values are deleted at the end" "$(grep -q "DELETE /agent/decision.prompt_profile?experiment_id=${exp2}&arm=A" "${FAKE_FLAG_LOG}" &&
  grep -q "DELETE /agent/decision.prompt_profile?experiment_id=${exp2}&arm=B" "${FAKE_FLAG_LOG}" && no_arms && echo 0 || echo 1)"
ok "every game is bound to the experiment and its arm" "$([[ "$(grep 'compose run' "${WORK}/docker.log" | grep -c "SHADOW_GAME_EXPERIMENT_ID=${exp2}")" == 4 &&
  "$(grep 'compose run' "${WORK}/docker.log" | grep -o 'SHADOW_GAME_ARM=[AB]' | tr -d '\n')" == SHADOW_GAME_ARM=ASHADOW_GAME_ARM=BSHADOW_GAME_ARM=BSHADOW_GAME_ARM=A ]] && echo 0 || echo 1)"
ok "llm.eligible_game_kinds is written shadow_only (no remote call in a real game)" "$(grep 'PUT /agent/llm.eligible_game_kinds' "${FAKE_FLAG_LOG}" | grep -q '"variant": "shadow_only"' && echo 0 || echo 1)"
clear_all

# 3. SIGTERM mid-run restores the flags and the agent.
touch "${WORK}/hang"
seed_operator_overlays
: >"${WORK}/docker.log"
"${AB_RUN[@]}" --out "${WORK}/o3" >/dev/null 2>&1 &
pid=$!
wait_until 20 arms_written || true
ok "arm values set during the run" "$(arms_written && echo 0 || echo 1)"
kill -TERM "${pid}"
wait "${pid}" 2>/dev/null || true
ok "SIGTERM restored every overlay exactly and cleared the arm values" "$(operator_overlays_intact && no_arms && echo 0 || echo 1)"
ok "SIGTERM started the agent again" "$(grep -q 'compose start agent' "${WORK}/docker.log" && echo 0 || echo 1)"
clear_all

# 4. SIGKILL skips the trap: a second run is refused, `restore --out` repairs everything.
seed_operator_overlays
: >"${WORK}/docker.log"
"${AB_RUN[@]}" --out "${WORK}/o4" >/dev/null 2>&1 &
pid=$!
wait_until 20 arms_written || true
kill -KILL "${pid}"
wait "${pid}" 2>/dev/null || true
ok "SIGKILL left the experiment flags and arm values behind" "$([[ "$(ov_writer decision.backend)" == group-ab ]] && arms_written && echo 0 || echo 1)"
code=0
run_ab --out "${WORK}/o4" 2>/dev/null || code=$?
ok "a second run over the same --out is refused" "$([[ ${code} == 3 ]] && echo 0 || echo 1)"
code=0
run_ab --out "${WORK}/o4b" 2>/dev/null || code=$?
ok "a run over leftover group-ab overlays is refused, nothing more written" "$([[ ${code} == 3 && ! -e "${WORK}/o4b/snapshot.tsv" ]] && echo 0 || echo 1)"
rm -f "${WORK}/hang"
"${AB}" restore --out "${WORK}/o4" >/dev/null
ok "restore --out puts every overlay back exactly and clears the arm values" "$(operator_overlays_intact && no_arms && [[ -e "${WORK}/o4/experiment.cleared" && ! -e "${WORK}/o4/experiment" ]] && echo 0 || echo 1)"
ok "restore --out removes group-ab containers and starts the agent" "$(grep -q 'rm -f cid1' "${WORK}/docker.log" && grep -q 'compose start agent' "${WORK}/docker.log" && echo 0 || echo 1)"
code=0
"${AB}" restore --out "${WORK}/o4" >/dev/null 2>&1 || code=$?
ok "restore --out twice is harmless" "$([[ ${code} == 0 ]] && operator_overlays_intact && echo 0 || echo 1)"
clear_all

# 5. an operator change during the run survives the restore.
touch "${WORK}/hang"
seed_operator_overlays
"${AB_RUN[@]}" --out "${WORK}/o5" >/dev/null 2>&1 &
pid=$!
wait_until 20 arms_written || true
"${FLAGS}" set agent decision.backend openai --writer operator >/dev/null
"${FLAGS}" clear agent experiment_dynamic_enabled >/dev/null
kill -TERM "${pid}"
wait "${pid}" 2>/dev/null || true
ok "the operator's new overlay survives" "$([[ "$(ov_spec decision.backend)" == variant:openai && "$(ov_writer decision.backend)" == operator ]] && echo 0 || echo 1)"
ok "an overlay the operator cleared stays cleared" "$([[ "$(ov_spec experiment_dynamic_enabled)" == none ]] && echo 0 || echo 1)"
ok "the other flags are restored" "$([[ "$(ov_spec interventions_allowed)" == variant:ambient && "$(ov_spec decision.prompt_profile)" == none ]] && no_arms && echo 0 || echo 1)"
clear_all

# 6. a real game appearing DURING a game aborts it at once (polled), restores and exits 4.
touch "${WORK}/hang"
echo '[{"game_id":"s","game_kind":"shadow"}]' >"${WORK}/games.json"
: >"${WORK}/docker.log"
seed_operator_overlays
"${AB_RUN[@]}" --out "${WORK}/o6" >/dev/null 2>&1 &
pid=$!
wait_until 20 grep -q 'compose run' "${WORK}/docker.log" || true
echo '[{"game_id":"r","game_kind":"real"}]' >"${WORK}/games.json"
code=0
wait "${pid}" 2>/dev/null || code=$?
ok "a real game mid-game aborts with exit 4" "$([[ ${code} == 4 ]] && echo 0 || echo 1)"
ok "the abort removed the container, restored the flags and started the agent" "$(grep -q 'rm -f group-ab-' "${WORK}/docker.log" && operator_overlays_intact && grep -q 'compose start agent' "${WORK}/docker.log" && echo 0 || echo 1)"
echo '[{"game_id":"s","game_kind":"shadow"}]' >"${WORK}/games.json"
rm -f "${WORK}/hang"
clear_all

# 7. a failed arm write stops the run before any game.
cat >"${WORK}/failprofile.sh" <<SH
#!/usr/bin/env bash
[[ "\$*" == *"set agent decision.prompt_profile"* ]] && exit 1
exec "${FLAGS}" "\$@"
SH
chmod +x "${WORK}/failprofile.sh"
code=0
FLAGS_SH="${WORK}/failprofile.sh" run_ab --out "${WORK}/o7" >/dev/null 2>&1 || code=$?
ok "a failed arm write stops the run (exit 3) before any game, flags restored" "$([[ ${code} == 3 && ! -s "${WORK}/o7/manifest.jsonl" ]] && all_clear && no_arms && echo 0 || echo 1)"
clear_all

# 7b. an agent image that ignores SHADOW_GAME_EXPERIMENT_ID / SHADOW_GAME_ARM (older than #2530): its record
#     has no binding, so the arm's profile never reached the game; the run stops at the first game.
touch "${WORK}/old-agent"
code=0
run_ab --out "${WORK}/o7b" 2>"${WORK}/err7b" || code=$?
rm -f "${WORK}/old-agent"
ok "a record without the experiment binding stops the run (exit 3), nothing in the manifest, flags restored" "$([[ ${code} == 3 && ! -s "${WORK}/o7b/manifest.jsonl" ]] && all_clear && no_arms && grep -q 'SHADOW_GAME_EXPERIMENT_ID' "${WORK}/err7b" && echo 0 || echo 1)"
clear_all

# 8. first-game verification: the arm's profile must show up on an agent.decide span of the game.
python3 - "${WORK}" <<'PY'
import json, sys


def doc(*profiles):
    spans = []
    for profile in profiles:
        spans.append(span(profile))
    return {"result": {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}}


def span(profile):
    attrs = [
        {"key": "game.id", "value": {"stringValue": "gfix"}},
        {"key": "decide.prompt_profile", "value": {"stringValue": profile}},
    ]
    return {"name": "agent.decide", "startTimeUnixNano": "1", "endTimeUnixNano": "2", "attributes": attrs}


json.dump(doc("v1", "neutral"), open(sys.argv[1] + "/seen.json", "w"))
json.dump(doc("v1"), open(sys.argv[1] + "/only-v1.json", "w"))
json.dump(doc("neutral"), open(sys.argv[1] + "/wrong.json", "w"))
PY
"${AB}" run --archetype slow_cascade --seeds 1 --reps 1 --traces-file "${WORK}/seen.json" --out "${WORK}/o8" >/dev/null 2>&1
ok "verification passes when each arm's profile is on an agent.decide span" "$([[ "$(wc -l <"${WORK}/o8/manifest.jsonl")" == 2 ]] && echo 0 || echo 1)"
clear_all
code=0
AB_VERIFY_TRIES=2 AB_VERIFY_WAIT=0.1 "${AB}" run --archetype slow_cascade --seeds 1 --reps 1 --traces-file "${WORK}/wrong.json" --out "${WORK}/o9" >/dev/null 2>&1 || code=$?
ok "verification stops the run when no span carries the profile (exit 3), flags restored" "$([[ ${code} == 3 && "$(wc -l <"${WORK}/o9/manifest.jsonl")" == 1 ]] && all_clear && echo 0 || echo 1)"
clear_all
# a lost arm-B value: arm A (the flag default v1) verifies, the first B game does not, and the run stops
# after game 2 instead of after the whole budget
code=0
AB_VERIFY_TRIES=2 AB_VERIFY_WAIT=0.1 "${AB}" run --archetype slow_cascade --seeds 1,2 --reps 1 --traces-file "${WORK}/only-v1.json" --out "${WORK}/o9b" >/dev/null 2>&1 || code=$?
ok "verification also checks the first B game: only v1 spans stop the run at game 2 (exit 3), flags restored" "$([[ ${code} == 3 && "$(wc -l <"${WORK}/o9b/manifest.jsonl")" == 2 ]] && all_clear && echo 0 || echo 1)"
clear_all

# 10. kill switch: a stack-scope none, an unreadable kill-switch read, a real game after the last game, SIGHUP.
"${FLAGS}" set agent interventions_allowed none --writer operator >/dev/null
code=0
run_ab --out "${WORK}/o11" 2>/dev/null || code=$?
ok "refuses over a stack-scope interventions_allowed=none (exit 4)" "$([[ ${code} == 4 ]] && echo 0 || echo 1)"
clear_all
cat >"${WORK}/failget.sh" <<SH
#!/usr/bin/env bash
[[ "\$*" == "get agent interventions_allowed" ]] && exit 1
exec "${FLAGS}" "\$@"
SH
chmod +x "${WORK}/failget.sh"
code=0
FLAGS_SH="${WORK}/failget.sh" run_ab --out "${WORK}/o12" 2>/dev/null || code=$?
ok "an unreadable kill-switch read refuses (exit 3, fail closed), nothing written" "$([[ ${code} == 3 && ! -e "${WORK}/o12/snapshot.tsv" ]] && all_clear && echo 0 || echo 1)"
touch "${WORK}/flip-on-b"
code=0
run_ab --out "${WORK}/o13" >/dev/null 2>&1 || code=$?
rm -f "${WORK}/flip-on-b"
echo '[{"game_id":"s","game_kind":"shadow"}]' >"${WORK}/games.json"
ok "a real game after the last game ends the run with exit 4, flags restored" "$([[ ${code} == 4 && "$(wc -l <"${WORK}/o13/manifest.jsonl")" == 2 ]] && all_clear && echo 0 || echo 1)"
clear_all
touch "${WORK}/hang"
: >"${WORK}/docker.log"
seed_operator_overlays
# A detached caller (nohup, a CI scope) hands down an ignored SIGHUP, which a shell cannot trap: reset it first.
python3 -c 'import os, signal, sys; signal.signal(signal.SIGHUP, signal.SIG_DFL); os.execv(sys.argv[1], sys.argv[1:])' \
  "${AB_RUN[@]}" --out "${WORK}/o14" >/dev/null 2>&1 &
pid=$!
wait_until 20 arms_written || true
kill -HUP "${pid}"
code=0
wait "${pid}" 2>/dev/null || code=$?
rm -f "${WORK}/hang"
ok "SIGHUP restores every overlay, starts the agent and exits 129" "$([[ ${code} == 129 ]] && operator_overlays_intact && grep -q 'compose start agent' "${WORK}/docker.log" && echo 0 || echo 1)"
clear_all

# 9. the report keeps a game only when decide.prompt_profile matches its arm.
cat >"${WORK}/man.jsonl" <<'JSON'
{"arm":"A","profile":"v1","seed":"1","rep":1,"game_id":"gA","record":{"outcome":"winner","winner_slot":0}}
{"arm":"B","profile":"neutral","seed":"1","rep":1,"game_id":"gB","record":{"outcome":"winner","winner_slot":1}}
{"arm":"B","profile":"neutral","seed":"2","rep":1,"game_id":"gX","record":{"outcome":"winner","winner_slot":1}}
JSON
python3 - "${WORK}" <<'PY'
import json, sys
def span(game, name, start, end, **attrs):
    def v(x):
        if isinstance(x, bool): return {"boolValue": x}
        if isinstance(x, int): return {"intValue": str(x)}
        return {"stringValue": x}
    a = [{"key": "game.id", "value": {"stringValue": game}}] + [{"key": k.replace("_", "."), "value": v(x)} for k, x in attrs.items()]
    return {"name": name, "startTimeUnixNano": str(start), "endTimeUnixNano": str(end), "attributes": a}
def attrs_raw(game, name, start, end, kv):
    s = span(game, name, start, end)
    for k, x in kv.items():
        s["attributes"].append({"key": k, "value": ({"boolValue": x} if isinstance(x, bool) else {"intValue": str(x)} if isinstance(x, int) else {"stringValue": x})})
    return s
S = 10**9
T = 1_700_000_000 * S
spans = [
    attrs_raw("gA", "agent.decide", T, T + 100 * S, {"decide.prompt_profile": "v1"}),
    attrs_raw("gA", "agent.decision", T + 90 * S, T + 91 * S, {"decision.action": "grant_shield", "decision.applied": True, "decision.blocked": False}),
    attrs_raw("gB", "agent.decide", T, T + 100 * S, {"decide.prompt_profile": "neutral"}),
    attrs_raw("gB", "agent.decision", T + 80 * S, T + 81 * S, {"decision.action": "grant_group_shield", "decision.applied": True, "decision.blocked": False, "decision.group.alive": 3, "decision.group.size": 3}),
    attrs_raw("gX", "agent.decide", T, T + 100 * S, {"decide.prompt_profile": "v1"}),
]
json.dump({"result": {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}}, open(sys.argv[1] + "/traces.json", "w"))
PY
mkdir -p "${WORK}/o10"
cp "${WORK}/man.jsonl" "${WORK}/o10/manifest.jsonl"
"${AB}" report --out "${WORK}/o10" --traces-file "${WORK}/traces.json" --json >"${WORK}/report.json"
python3 - "${WORK}/report.json" <<'PY' || fails=$((fails + 1))
import json, sys
r = json.load(open(sys.argv[1]))
assert r["A"]["single_applied"] == 1 and r["A"]["group_applied"] == 0, r["A"]
assert r["B"]["group_applied"] == 1 and r["B"]["group_endgame"] == 1 and r["B"]["group_late"] == 1, r["B"]
assert r["B"]["games"] == 1 and r["B"]["excluded"] == {"profile_mismatch:v1": 1}, r["B"]
print("ok   report filters on decide.prompt_profile and counts group and single grants")
PY

if ((fails > 0)); then echo "${fails} test(s) failed"; exit 1; fi
echo "all agent-group-ab tests passed"
