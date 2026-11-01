#!/usr/bin/env bash
# Self-contained tests for scripts/agent-group-ab.sh (#2467 PR 6): the live-game guard, the
# flag snapshot and restore (also on SIGTERM), the A,B,B,A order and the report's
# decide.prompt_profile filter. Fake flag service + stubbed docker, no stack.
#
# UC: UC-040 A47 BR-033 BR-036
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AB="${SCRIPT_DIR}/../agent-group-ab.sh"
WORK="$(mktemp -d)"
# shellcheck source=scripts/tests/lib-fake-flag-service.sh
source "${SCRIPT_DIR}/lib-fake-flag-service.sh"
trap 'stop_fake_flag_service; rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
echo '[]' >"${WORK}/games.json"
start_fake_flag_service "${WORK}" --games-file "${WORK}/games.json"
fails=0
check() { # check <name> <condition-exit-code>
  if [[ "$2" == 0 ]]; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi
}
overlay_of() { python3 "${SCRIPT_DIR}/../agent_group_ab.py" overlay <<<"$("${SCRIPT_DIR}/../flags.sh" get agent "$1")"; }

# Stub docker: `compose` records its args (ps answers "running" for the agent), `logs` prints a
# replay record whose game_id carries the container name, `rm` is a no-op.
cat >"${WORK}/bin/docker" <<SH
#!/usr/bin/env bash
echo "\$*" >>"${WORK}/docker.log"
case "\$1" in
  compose) [[ "\$2" == ps ]] && echo abc123 ;;
  logs) [[ -e "${WORK}/hang" ]] && sleep 30
        echo "PATTERN_REPLAY_RECORD {\"game_id\":\"\$2\",\"outcome\":\"winner\",\"winner_slot\":1}" ;;
esac
exit 0
SH
chmod +x "${WORK}/bin/docker"
export PATH="${WORK}/bin:${PATH}"
export PATTERN_REPLAY_TIMEOUT=20

# 1. a live real game refuses the run and touches nothing.
echo '[{"game_id":"g1","game_kind":"real"}]' >"${WORK}/games.json"
code=0
"${AB}" run --archetype statue_finale --seeds 1 --reps 1 --out "${WORK}/o1" 2>"${WORK}/err1" || code=$?
check "refuses while a real game is live (exit 4)" "$([[ ${code} == 4 ]] && echo 0 || echo 1)"
check "refusal changed no flag" "$([[ "$(overlay_of decision.prompt_profile)" == none ]] && echo 0 || echo 1)"
check "refusal did not stop the agent" "$([[ ! -e "${WORK}/docker.log" ]] && echo 0 || echo 1)"
echo '[{"game_id":"g2","game_kind":"shadow"}]' >"${WORK}/games.json"

# 2. a full run: order A,B,B,A, flags restored, agent stopped and restarted, group on in both arms.
"${AB}" run --archetype statue_finale --seeds 7 --reps 2 --backend claude --out "${WORK}/o2" >"${WORK}/out2"
order="$(python3 -c 'import json,sys; print("".join(json.loads(l)["arm"] for l in open(sys.argv[1])))' "${WORK}/o2/manifest.jsonl")"
check "games run A,B,B,A" "$([[ "${order}" == ABBA ]] && echo 0 || echo 1)"
for f in decision.prompt_profile decision.backend interventions_allowed llm.eligible_game_kinds policy.group_grant.enabled; do
  check "restored ${f}" "$([[ "$(overlay_of "${f}")" == none ]] && echo 0 || echo 1)"
done
check "agent stopped then started" "$(grep -q 'compose stop agent' "${WORK}/docker.log" && grep -q 'compose start agent' "${WORK}/docker.log" && echo 0 || echo 1)"
check "group lever on and shadow_group set during the run" "$(grep -q 'policy.group_grant.enabled' "${FAKE_FLAG_LOG}" && grep -q 'shadow_group' "${FAKE_FLAG_LOG}" && echo 0 || echo 1)"
check "real_games never written" "$(grep -q 'PUT.*real_games' "${FAKE_FLAG_LOG}" && echo 1 || echo 0)"
check "seed passed to the one-shot runner" "$(grep -q 'SHADOW_GAME_SEED=7' "${WORK}/docker.log" && echo 0 || echo 1)"

# 3. SIGTERM mid-run restores the flags.
touch "${WORK}/hang"
"${AB}" run --archetype statue_finale --seeds 1 --reps 1 --out "${WORK}/o3" >/dev/null 2>&1 &
pid=$!
for _ in $(seq 1 100); do
  [[ "$(overlay_of decision.prompt_profile)" == variant:v1 ]] && break
  sleep 0.1
done
check "profile set during the run" "$([[ "$(overlay_of decision.prompt_profile)" == variant:v1 ]] && echo 0 || echo 1)"
kill -TERM "${pid}"
wait "${pid}" 2>/dev/null || true
rm -f "${WORK}/hang"
check "SIGTERM restored the profile" "$([[ "$(overlay_of decision.prompt_profile)" == none ]] && echo 0 || echo 1)"
check "SIGTERM restored the group flag" "$([[ "$(overlay_of policy.group_grant.enabled)" == none ]] && echo 0 || echo 1)"

# 4. a real game appearing between games restores and aborts.
rm -rf "${WORK}/o4"
cat >"${WORK}/bin/docker" <<SH
#!/usr/bin/env bash
echo "\$*" >>"${WORK}/docker.log"
case "\$1" in
  compose) [[ "\$2" == ps ]] && echo abc123 ;;
  logs) echo '[{"game_id":"r","game_kind":"real"}]' >"${WORK}/games.json"
        echo "PATTERN_REPLAY_RECORD {\"game_id\":\"\$2\"}" ;;
esac
exit 0
SH
code=0
"${AB}" run --archetype statue_finale --seeds 1 --reps 1 --out "${WORK}/o4" >/dev/null 2>"${WORK}/err4" || code=$?
check "a real game between games aborts (exit 4)" "$([[ ${code} == 4 ]] && echo 0 || echo 1)"
check "abort restored the profile" "$([[ "$(overlay_of decision.prompt_profile)" == none ]] && echo 0 || echo 1)"
check "abort left one game in the manifest" "$([[ "$(wc -l <"${WORK}/o4/manifest.jsonl")" == 1 ]] && echo 0 || echo 1)"

# 5. the report keeps a game only when decide.prompt_profile matches its arm.
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
mkdir -p "${WORK}/o5"
cp "${WORK}/man.jsonl" "${WORK}/o5/manifest.jsonl"
"${AB}" report --out "${WORK}/o5" --traces-file "${WORK}/traces.json" --json >"${WORK}/report.json"
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
