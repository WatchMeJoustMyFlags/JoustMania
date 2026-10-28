#!/usr/bin/env bash
# Tests for the "replay gives no record" exits of scripts/pattern-replay-check.sh (#1703, #1725).
# `docker` is faked on PATH and the flag helper stubbed (FLAGS_SH); no stack needed, runs in seconds.
#
# UC: UC-046 A16
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

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

# Hermetic: every case runs with a fake `docker` first on PATH and a stub flag helper,
# so nothing here can reach a real stack, container or flag service. The fake docker
# fails loudly (exit 99, marker file) unless a case opts in with FAKE_DOCKER_OK=1; only
# the two "no record" cases opt in, and their docker never prints a record.
cat >"${WORK}/bin/docker" <<'SH'
#!/usr/bin/env bash
echo "docker $*" >>"${FAKE_DOCKER_LOG}"
if [[ "${FAKE_DOCKER_OK:-}" != 1 ]]; then
  echo "FAKE DOCKER CALLED UNEXPECTEDLY: docker $*" >&2
  touch "${FAKE_DOCKER_LOG}.unexpected"
  exit 99
fi
exit 0
SH
chmod +x "${WORK}/bin/docker"
export FAKE_DOCKER_LOG="${WORK}/docker.log"
: >"${FAKE_DOCKER_LOG}"
export PATH="${WORK}/bin:${PATH}"
# An exported COMPOSE/DOCKER would bypass the fake docker (run_once uses ${COMPOSE:-docker compose}).
unset COMPOSE DOCKER
cat >"${WORK}/flags.sh" <<'SH'
#!/usr/bin/env bash
v="${STUB_VARIANT:-default}"
overlay="${STUB_OVERLAY:+,\"stack_overlay\":${STUB_OVERLAY}}"
[[ "${STUB_FAIL:-}" == 1 ]] && exit 1
echo "{\"effective_default\":\"${v}\",\"variant_values\":{\"default\":300,\"long\":600,\"soak\":1800}${overlay}}"
SH
chmod +x "${WORK}/flags.sh"
export FLAGS_SH="${WORK}/flags.sh"

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
expect "replay prints no record" 3 "no PATTERN_REPLAY_RECORD" -- env FAKE_DOCKER_OK=1 PATTERN_REPLAY_TIMEOUT=1 "${CHECK}" --against "${WORK}/ok.json"
expect "two-run flow prints no record" 3 "no PATTERN_REPLAY_RECORD" -- env FAKE_DOCKER_OK=1 PATTERN_REPLAY_TIMEOUT=1 "${CHECK}" slow_cascade 7
expect "compare usage" 3 "usage" -- "${CHECK}" --compare "${WORK}/ok.json"
expect "against usage" 3 "usage" -- "${CHECK}" --against

# The wait follows the pinned safety cap (default 300, long 600, soak 1800) plus 120 s.
expect "wait: default cap" 0 "420" -- "${CHECK}" --show-timeout
expect "wait: long cap" 0 "720" -- env STUB_VARIANT=long "${CHECK}" --show-timeout
expect "wait: soak cap" 0 "1920" -- env STUB_VARIANT=soak "${CHECK}" --show-timeout
expect "wait: overlay value" 0 "1020" -- env STUB_OVERLAY='{"value":900}' "${CHECK}" --show-timeout
expect "wait: out-of-range cap falls back to 300" 0 "420" -- env STUB_OVERLAY='{"value":5000}' "${CHECK}" --show-timeout
expect "wait: flag service unreachable" 0 "420" -- env STUB_FAIL=1 "${CHECK}" --show-timeout
expect "wait: env override wins" 0 "55" -- env STUB_VARIANT=soak PATTERN_REPLAY_TIMEOUT=55 "${CHECK}" --show-timeout
expect "wait: env override not a number" 3 "positive integer" -- env PATTERN_REPLAY_TIMEOUT=abc "${CHECK}" --show-timeout
expect "wait: env override zero" 3 "positive integer" -- env PATTERN_REPLAY_TIMEOUT=0 "${CHECK}" --show-timeout
expect "wait: env override negative" 3 "positive integer" -- env PATTERN_REPLAY_TIMEOUT=-5 "${CHECK}" --show-timeout
expect "wait: env override invalid on a run" 3 "positive integer" -- env FAKE_DOCKER_OK=1 PATTERN_REPLAY_TIMEOUT=1x "${CHECK}" slow_cascade 7

# --show-timeout and every error exit above must never reach docker.
if [[ -e "${FAKE_DOCKER_LOG}.unexpected" ]]; then
  echo "FAIL a case called docker unexpectedly:"
  grep -v '^$' "${FAKE_DOCKER_LOG}" | head -n 5
  fails=$((fails + 1))
fi

if ((fails > 0)); then
  echo "${fails} test(s) failed"
  exit 1
fi
echo "all A16 tests passed"
