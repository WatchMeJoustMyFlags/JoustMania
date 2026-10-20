#!/usr/bin/env bash
# Tests for the "replay gives no record" exits of scripts/pattern-replay-check.sh (#1703, #1725).
# `docker` is stubbed on PATH; no stack needed.
#
# UC: UC-046 A16
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="${SCRIPT_DIR}/../pattern-replay-check.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
fails=0

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

# A docker whose runs never print a record.
cat >"${WORK}/bin/docker" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "${WORK}/bin/docker"

echo '{"params_digest":"abc","rng_seed":"7","flag_digest":"fd","replay_exact":true}' >"${WORK}/plain-no-archetype.json"
expect "plain record without archetype" 3 "record has no archetype" -- "${CHECK}" --against "${WORK}/plain-no-archetype.json"
echo '{"archetype":"","rng_seed":"7"}' >"${WORK}/empty-archetype.json"
expect "empty archetype" 3 "record has no archetype" -- "${CHECK}" --against "${WORK}/empty-archetype.json"
echo '{"seq":9,"kind":"pattern_game_ended","game_id":"g","pattern":{"rng_seed":7}}' >"${WORK}/journal-no-archetype.jsonl"
expect "journal record without archetype" 3 "record has no archetype" -- "${CHECK}" --against "${WORK}/journal-no-archetype.jsonl"
printf "%s\n%s\n" '{"seq":1,"kind":"game_assigned"}' '{"seq":2,"kind":"x"}' >"${WORK}/journal-no-event.jsonl"
expect "journal without pattern_game_ended" 3 "no pattern_game_ended" -- "${CHECK}" --against "${WORK}/journal-no-event.jsonl"
expect "unreadable record" 3 "" -- "${CHECK}" --against "${WORK}/missing.json"
echo '{"archetype":"slow_cascade","rng_seed":"7"}' >"${WORK}/ok.json"
expect "replay prints no record" 3 "no PATTERN_REPLAY_RECORD" -- env PATH="${WORK}/bin:${PATH}" PATTERN_REPLAY_TIMEOUT=1 "${CHECK}" --against "${WORK}/ok.json"
expect "two-run flow prints no record" 3 "no PATTERN_REPLAY_RECORD" -- env PATH="${WORK}/bin:${PATH}" PATTERN_REPLAY_TIMEOUT=1 "${CHECK}" slow_cascade 7
expect "compare usage" 3 "usage" -- "${CHECK}" --compare "${WORK}/ok.json"
expect "against usage" 3 "usage" -- "${CHECK}" --against

if ((fails > 0)); then
  echo "${fails} test(s) failed"
  exit 1
fi
echo "all A16 tests passed"
