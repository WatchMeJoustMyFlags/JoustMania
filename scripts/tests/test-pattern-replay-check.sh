#!/usr/bin/env bash
# Self-contained tests for scripts/pattern-replay-check.sh (UC-046, #1258).
#
# Exercises the record comparison (--compare) and the two-run flow with `docker`
# stubbed on PATH, so no stack is needed. Runs as part of `make lint`, or directly:
#   ./scripts/tests/test-pattern-replay-check.sh
#
# UC: UC-046 main step 9 step 10 A11
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="${SCRIPT_DIR}/../pattern-replay-check.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
fails=0

record() { # record <file> <checkpoint-of-slot1-second2> [exact] [order] [flag_digest]
  cat >"$1" <<JSON
{"game_id":"g","archetype":"slow_cascade","params_digest":"abc","rng_seed":"7","flag_digest":"${5:-fd}",
 "outcome":"winner","elimination_order":${4:-[2,1]},"winner_slot":0,
 "signal_hashes":["h0","h1","h2"],
 "signal_checkpoints":[["a","b","c"],["d","$2"],["f"]],
 "replay_exact":${3:-true},"non_exact_reasons":[],"decisions":["send_controller_effect:2"]}
JSON
}

expect() { # expect <name> <want-exit> <want-substring> -- cmd...
  local name="$1" want="$2" sub="$3"
  shift 4
  local out code=0
  out="$("$@" 2>&1)" || code=$?
  if [[ "${code}" != "${want}" || "${out}" != *"${sub}"* ]]; then
    echo "FAIL ${name}: exit ${code} (want ${want}), output: ${out}"
    fails=$((fails + 1))
  else
    echo "ok   ${name}"
  fi
}

record "${WORK}/a.json" e
record "${WORK}/b.json" e
expect "identical records" 0 "identical" -- "${CHECK}" --compare "${WORK}/a.json" "${WORK}/b.json"

# A shorter run (a slot left the stream earlier) still matches on the common prefix.
cat >"${WORK}/short.json" <<'JSON'
{"archetype":"slow_cascade","params_digest":"abc","flag_digest":"fd","outcome":"winner",
 "elimination_order":[2,1],"winner_slot":0,"signal_checkpoints":[["a","b"],["d"],["f"]],
 "replay_exact":true,"decisions":["send_controller_effect:2"]}
JSON
expect "common prefix" 0 "identical" -- "${CHECK}" --compare "${WORK}/a.json" "${WORK}/short.json"

record "${WORK}/c.json" X
expect "checkpoint differs" 1 "slot 1 checkpoint at second 2" -- "${CHECK}" --compare "${WORK}/a.json" "${WORK}/c.json"
record "${WORK}/d.json" e true "[1,2]"
expect "order differs" 1 "elimination_order" -- "${CHECK}" --compare "${WORK}/a.json" "${WORK}/d.json"
record "${WORK}/n.json" e false
expect "non-exact" 2 "non-exact" -- "${CHECK}" --compare "${WORK}/a.json" "${WORK}/n.json"
record "${WORK}/f.json" e true "[2,1]" other
expect "flag digest differs" 2 "flag_digest differs" -- "${CHECK}" --compare "${WORK}/a.json" "${WORK}/f.json"
sed 's/send_controller_effect:2/grant_shield:1/' "${WORK}/a.json" >"${WORK}/dec.json"
expect "decision differs" 1 "decision 0" -- "${CHECK}" --compare "${WORK}/a.json" "${WORK}/dec.json"
sed 's/"params_digest":"abc"/"params_digest":"xyz"/' "${WORK}/a.json" >"${WORK}/p.json"
expect "params digest differs" 2 "params_digest differs" -- "${CHECK}" --compare "${WORK}/a.json" "${WORK}/p.json"
expect "usage" 3 "usage" -- "${CHECK}"

# Two-run flow with docker stubbed: `run` succeeds, `logs` prints the record.
# Hermetic: nothing here may reach a real docker, compose or flag service. Any exported
# COMPOSE/DOCKER would bypass the stub, and the flag helper is stubbed (no live GET).
unset COMPOSE DOCKER
cat >"${WORK}/flags.sh" <<'SH'
#!/usr/bin/env bash
echo '{"effective_default":"default","variant_values":{"default":300}}'
SH
chmod +x "${WORK}/flags.sh"
export FLAGS_SH="${WORK}/flags.sh"
# Fail-loud docker first on PATH for every case; the two-run cases below replace it with the stub.
cat >"${WORK}/bin/docker" <<'SH'
#!/usr/bin/env bash
echo "FAKE DOCKER CALLED UNEXPECTEDLY: docker $*" >&2
exit 99
SH
chmod +x "${WORK}/bin/docker"
export PATH="${WORK}/bin:${PATH}"

cat >"${WORK}/bin/docker" <<SH
#!/usr/bin/env bash
case "\$1" in
  compose) printf '%s\n' "\$@" >>"${WORK}/compose-args" ;;
  logs) echo "time=x level=INFO msg=start"; echo "PATTERN_REPLAY_RECORD \$(tr -d '\n' <"${WORK}/a.json")" ;;
esac
exit 0
SH
chmod +x "${WORK}/bin/docker"
expect "two runs" 0 "identical" -- env PATH="${WORK}/bin:${PATH}" "${CHECK}" slow_cascade 7
if ! grep -q "SHADOW_GAME_ARCHETYPE=slow_cascade" "${WORK}/compose-args" || ! grep -q "SHADOW_GAME_SEED=7" "${WORK}/compose-args"; then
  echo "FAIL two runs: archetype/seed not passed to the one-shot runner"
  fails=$((fails + 1))
fi

# --against: a journal pattern_game_ended line is replayed with its archetype and seed.
cat >"${WORK}/journal.jsonl" <<'JSON'
{"seq":1,"kind":"game_assigned","game_id":"g"}
{"seq":9,"kind":"pattern_game_ended","game_id":"g","pattern":{"archetype":"slow_cascade","params_digest":"abc","rng_seed":7,"flag_digest":"fd","outcome":"winner","elimination_order":[2,1],"winner_slot":0,"signal_checkpoints":[["a","b","c"],["d","e"],["f"]],"replay_exact":true,"decisions":["send_controller_effect:2"]}}
JSON
: >"${WORK}/compose-args"
expect "against journal event" 0 "identical" -- env PATH="${WORK}/bin:${PATH}" "${CHECK}" --against "${WORK}/journal.jsonl"
if ! grep -q "SHADOW_GAME_SEED=7" "${WORK}/compose-args"; then
  echo "FAIL against: recorded seed not passed to the one-shot runner"
  fails=$((fails + 1))
fi
expect "compare journal vs record" 0 "identical" -- "${CHECK}" --compare "${WORK}/journal.jsonl" "${WORK}/a.json"
record "${WORK}/u.json" e true "[2,1]" unavailable
expect "unavailable digest" 2 "unavailable" -- "${CHECK}" --compare "${WORK}/u.json" "${WORK}/u.json"

if ((fails > 0)); then
  echo "${fails} test(s) failed"
  exit 1
fi
echo "all pattern-replay-check tests passed"
