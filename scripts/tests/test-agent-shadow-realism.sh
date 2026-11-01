#!/usr/bin/env bash
# Self-contained tests for scripts/agent-shadow-realism.sh (#1017): the per-metric verdicts
# (similar, divergent, insufficient; insufficient never counts as similar), the objective roll-up,
# game placement from the duration gauge's first sample (a stale series outlives its game), the
# death counter increase per container, the Jaeger near-death cross-check (one summary per player),
# the mode filter, and that the tool only sends GET requests to the two query APIs. A fake
# VictoriaMetrics + Jaeger (scripts/tests/fake_vm_jaeger.py), no stack.
#
# UC: UC-043 A22
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SR="${SCRIPT_DIR}/../agent-shadow-realism.sh"
TOOL="${SCRIPT_DIR}/../agent_shadow_realism.py"
WORK="$(mktemp -d)"
FAKE_PID=""
trap '[[ -n "${FAKE_PID}" ]] && kill "${FAKE_PID}" 2>/dev/null; rm -rf "${WORK}"' EXIT
fails=0
ok() { # ok <name> <exit code of the condition>
  if [[ "$2" == 0 ]]; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi
}
cond() { if "$@"; then echo 0; else echo 1; fi; }

T0=1700000000
START=$((T0 - 60))
END=$((T0 + 40000))

# scenario <file> <real n> <shadow n> [shadow duration factor] [extra: fightclub|linger]
scenario() {
  python3 - "$@" <<'PY'
import json, sys
out, n_real, n_shadow = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
factor = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
extra = sys.argv[5:] if len(sys.argv) > 5 else []
T0 = 1700000000
games, t = [], T0

def game(kind, i, prefix, f, mode="FFA", jaeger=True):
    global t
    dur = (40 + 10 * (i % 5)) * f
    serials = [f"{prefix}{i}S{j}" for j in range(4)]
    g = {"id": f"game_{prefix}{i:04d}", "kind": kind, "mode": mode, "start": t, "dur": dur, "threshold": 2.8,
         "serials": serials,
         "accel": {s: 1.0 + 0.2 * j for j, s in enumerate(serials)},
         "variance": {s: 0.1 * (j + 1) for j, s in enumerate(serials)},
         "skill": {s: 0.2 * j for j, s in enumerate(serials)},
         "elim": {s: dur * j / 4 for j, s in enumerate(serials) if j},
         "near_deaths": 2 + i % 3,
         "jaeger_near_deaths": {s: 1 for s in serials} if jaeger else None}
    t += int(dur) + 30
    return g

for i in range(n_real):
    games.append(game("primary", i, "r", 1.0))
for i in range(n_shadow):
    games.append(game("shadow", i, "s", factor, jaeger=False))
if "fightclub" in extra:
    games += [game("primary", 900 + i, "fc", 1.0, mode="Fight Club") for i in range(2)]
if "linger" in extra:
    games[0]["linger"] = 10 ** 6  # the stale series is still exported long after the window
deaths = [{"game_kind": "primary", "mode": "FFA", "instance": "a", "before": 100, "max": 130},
          {"game_kind": "primary", "mode": "FFA", "instance": "b", "before": None, "max": 20},
          {"game_kind": "primary", "mode": "Fight Club", "instance": "b", "before": None, "max": 7},
          {"game_kind": "shadow", "mode": "FFA", "instance": "c", "before": None, "max": 10}]
json.dump({"games": games, "deaths": deaths}, open(out, "w"))
PY
}

SCEN="${WORK}/scenario.json"
LOG="${WORK}/requests.log"
: >"${LOG}"
scenario "${SCEN}" 10 10
python3 "${SCRIPT_DIR}/fake_vm_jaeger.py" --scenario "${SCEN}" --log "${LOG}" --port-file "${WORK}/port" &
FAKE_PID=$!
for _ in $(seq 1 100); do [[ -s "${WORK}/port" ]] && break; sleep 0.05; done
BASE="http://127.0.0.1:$(cat "${WORK}/port")"
run_sr() { "${SR}" --start "${START}" --end "${END}" --vm-url "${BASE}/victoria-metrics" --jaeger-url "${BASE}/jaeger/api" "$@"; }
# verdict <report json> <metric>
verdict() { python3 -c 'import json,sys; print({r["metric"]: r["verdict"] for r in json.load(open(sys.argv[1]))["metrics"]}[sys.argv[2]])' "$1" "$2"; }
objective() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["objectives"][sys.argv[2]]["realism"])' "$1" "$2"; }

# 1. shadow plays like real: every metric of both kinds similar, the objectives too.
run_sr --json --save "${WORK}/ds1.json" >"${WORK}/r1.json"
all_similar=0
for m in duration death_rate elimination_pace near_death_rate activity change closeness survival skill_gap intensity movement_variance; do
  v="$(verdict "${WORK}/r1.json" "${m}")"
  [[ "${m}" == death_rate ]] && continue # the counter fixture is not proportional to the games
  [[ "${v}" == similar ]] || { echo "  ${m}: ${v}"; all_similar=1; }
done
ok "identical real and shadow games: every metric similar" "${all_similar}"
ok "objectives similar when their fitness inputs are" "$(cond [ "$(objective "${WORK}/r1.json" endurance)" == similar -a "$(objective "${WORK}/r1.json" balanced)" == similar ])"
ok "no shadow trace in Jaeger: the cross-check is insufficient, not similar" "$(cond [ "$(verdict "${WORK}/r1.json" near_death_rate_jaeger)" == insufficient ])"

# 2. read-only: GET only, and only the two query APIs.
ok "only GET requests" "$(cond [ "$(grep -vc '^GET ' "${LOG}")" == 0 ])"
ok "only the VictoriaMetrics query API and Jaeger v3 traces" \
  "$(cond [ "$(grep -Evc '^GET /(victoria-metrics/api/v1/query(_range)?|jaeger/api/v3/traces)\?' "${LOG}")" == 0 ])"
ok "no docker call (the hermetic stub would exit 97) and no flag-service request" \
  "$(cond [ "$(grep -c '/flags' "${LOG}")" == 0 ])"

# 3. per-game values from the dataset: death counter, Jaeger dedupe, the game window.
python3 - "${WORK}/ds1.json" "${WORK}/r1.json" >"${WORK}/checks" <<'PY'
import json, sys
ds, rep = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
real = [g for g in ds["games"] if g["kind"] == "real"]
minutes = sum(g["duration"] for g in real) / 60
dr = next(r for r in rep["metrics"] if r["metric"] == "death_rate")
# instance a: 130 - 100; instance b (FFA 20 + Fight Club 7, both born in the window)
print("deaths", abs(dr["real"]["rate"] - 57 / minutes) < 1e-9)
g = real[0]
print("jaeger", abs(g["near_death_rate_jaeger"] - 4 / (4 * g["duration"] / 60)) < 1e-9)
print("vm_near", abs(g["near_death_rate"] - g["near_deaths"] / (4 * g["duration"] / 60)) < 1e-9)
print("window", g["start"] == 1700000000 and abs(g["end"] - (1700000000 + g["duration"])) < 1e-6)
print("survival", abs(g["survival"] - (0.25 + 0.5 + 0.75 + 1) / 4) < 0.02)
print("pace", abs(g["elimination_pace"] - 3 / (g["duration"] / 60)) < 1e-9)
print("activity", 0.4 < g["activity"] < 0.6)
PY
for c in deaths jaeger vm_near window survival pace activity; do
  ok "dataset: ${c}" "$(cond grep -qx "${c} True" "${WORK}/checks")"
done

# 4. a stale duration series that outlives the window: the game is placed by its first sample.
scenario "${SCEN}" 10 10 1 linger
run_sr --no-jaeger --save "${WORK}/ds4.json" >/dev/null
n4="$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["games"]))' "${WORK}/ds4.json")"
ok "a lingering duration gauge does not move its game out of the window" "$(cond [ "${n4}" == 20 ])"

# 5. shadow games three times as long: duration divergent, endurance and accelerate divergent.
scenario "${SCEN}" 10 10 3
run_sr --no-jaeger --json >"${WORK}/r5.json"
ok "longer shadow games: duration divergent" "$(cond [ "$(verdict "${WORK}/r5.json" duration)" == divergent ])"
ok "longer shadow games: endurance divergent" "$(cond [ "$(objective "${WORK}/r5.json" endurance)" == divergent ])"
ok "longer shadow games: intensity still similar" "$(cond [ "$(verdict "${WORK}/r5.json" intensity)" == similar ])"

# 6. too few shadow games: insufficient everywhere, never similar.
scenario "${SCEN}" 10 3
run_sr --no-jaeger --json >"${WORK}/r6.json"
ok "3 shadow games (min 8): duration insufficient" "$(cond [ "$(verdict "${WORK}/r6.json" duration)" == insufficient ])"
ok "3 shadow games: no objective similar" "$(cond [ "$(objective "${WORK}/r6.json" endurance)" == insufficient -a "$(objective "${WORK}/r6.json" balanced)" == insufficient ])"
run_sr --no-jaeger --json --min-n 3 >"${WORK}/r6b.json"
ok "--min-n 3 lets 3 games count" "$(cond [ "$(verdict "${WORK}/r6b.json" duration)" != insufficient ])"

# 7. the mode filter.
scenario "${SCEN}" 10 10 1 fightclub
run_sr --no-jaeger --mode FFA --save "${WORK}/ds7.json" >/dev/null
skipped="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["skipped"].get("other_mode", 0), len(d["games"]))' "${WORK}/ds7.json")"
ok "--mode FFA skips the Fight Club games" "$(cond [ "${skipped}" == "2 20" ])"

# 8. a saved dataset reports again; the text report names every metric.
"${SR}" --dataset "${WORK}/ds1.json" >"${WORK}/r8.txt"
ok "--dataset reports a saved dataset" "$(cond grep -q '^movement_variance ' "${WORK}/r8.txt")"
ok "text report rolls up the objectives" "$(cond grep -Eq '^  endurance +duration: similar' "${WORK}/r8.txt")"

# 9. errors: unreachable API exits 3, bad usage exits 2.
code=0
"${SR}" --since 1h --vm-url "http://127.0.0.1:9/victoria-metrics" --no-jaeger >/dev/null 2>&1 || code=$?
ok "unreachable VictoriaMetrics: exit 3" "$(cond [ "${code}" == 3 ])"
code=0
"${SR}" --since 3x >/dev/null 2>&1 || code=$?
ok "bad --since: exit 2" "$(cond [ "${code}" == 2 ])"

# 10. the distance measures.
ks="$(python3 -c "
import sys; sys.path.insert(0, '${SCRIPT_DIR}/..')
import agent_shadow_realism as t
print(t.ks_statistic([1,2,3],[4,5,6]), t.ks_statistic([1,2,3],[1,2,3]), t.ks_statistic([1,2,3,4],[3,4,5,6]),
      t.median_ratio(0,0), t.verdict(8, 8, 0.31, 1.0, 8, 0.3, 1.5), t.verdict(8, 8, 0.1, 1.6, 8, 0.3, 1.5),
      t.verdict(8, 7, 0.0, 1.0, 8, 0.3, 1.5))")"
ok "KS statistic, median ratio and verdict thresholds" "$(cond [ "${ks}" == "1.0 0.0 0.5 1.0 divergent divergent insufficient" ])"

if ((fails)); then
  echo "${fails} test(s) failed"
  exit 1
fi
echo "all agent-shadow-realism tests passed"
