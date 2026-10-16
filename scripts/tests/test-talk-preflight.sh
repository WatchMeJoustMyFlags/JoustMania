#!/usr/bin/env bash
# Self-contained tests for scripts/talk-preflight.sh (#743).
#
# Runs the preflight against a temp flag file with `docker`, `curl` and
# `timedatectl` stubbed on PATH, so no stack, network or daemon is needed.
#
# Runs as part of `make lint` (needs only bash + python3 + GNU date), or directly:
#   ./scripts/tests/test-talk-preflight.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT="${SCRIPT_DIR}/../talk-preflight.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin" "${WORK}/flags"
export AGENT_FLAG_FILE="${WORK}/flags/agent.json"
export TALK_CLOCK_URLS="https://clock.test"
REAL_DATE="$(command -v date)"
export PATH="${WORK}/bin:${PATH}"
SECRET="sk-test-DO-NOT-PRINT-123"

# docker stub: inspect answers from STUB_* env; everything else succeeds.
cat >"${WORK}/bin/docker" <<'SH'
#!/usr/bin/env bash
[[ "$1" == "info" ]] && exit 0
if [[ "$1" == "inspect" ]]; then
  [[ -n "${STUB_MISSING:-}" && "$4" == "${STUB_MISSING}" ]] && exit 1
  fmt="$3"
  case "${fmt}" in
    *State.Status*) echo "running healthy" ;;
    *Mounts*) echo "${STUB_MOUNT}" ;;
    *Config.Env*) printf '%s\n' "PATH=/usr/bin" "ANTHROPIC_API_KEY=${STUB_CLAUDE_KEY-${STUB_SECRET}}" "JEV_API_KEY=${STUB_JEV_KEY-${STUB_SECRET}}" "DECISION_OPENAI_API_KEY=" "AGENT_INFERENCE_BACKEND=${STUB_INFERENCE-claude}" "AGENT_LLM_MAX_REQUESTS_PER_MINUTE=${STUB_BUDGET:-}" ;;
  esac
  exit 0
fi
exit 0
SH
# curl stub: Jaeger traces JSON, a Date header, 200 for everything else.
cat >"${WORK}/bin/curl" <<'SH'
#!/usr/bin/env bash
for a in "$@"; do
  case "${a}" in
    *jaeger/api/traces*)
      echo '{"data":[{"spans":[{"operationName":"agent.decide","startTime":2,"tags":[{"key":"decide.backend","value":"claude"},{"key":"decide.outcome","value":"held"}]}]}]}'
      exit 0 ;;
    -sSI) echo "date: $(date -u -d "@$(( $(date +%s) - ${STUB_SKEW:-0} ))" '+%a, %d %b %Y %H:%M:%S GMT')"; exit 0 ;;
  esac
done
exit 0
SH
printf '#!/usr/bin/env bash\necho yes\n' >"${WORK}/bin/timedatectl"
# date stub: freezes `date +%s` (no second-boundary flakes), real date otherwise.
cat >"${WORK}/bin/date" <<SH
#!/usr/bin/env bash
if [[ "\$*" == "+%s" ]]; then echo 1760000000; exit 0; fi
exec "${REAL_DATE}" "\$@"
SH
chmod +x "${WORK}/bin/"*
export STUB_SECRET="${SECRET}"
export STUB_MOUNT="${WORK}/flags"

iv_fixture() {
  python3 - "${WORK}/flags/interventions.json" "$@" <<'PY'
import json, sys
path, overrides = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:])
def f(variants, default):
    return {"state": "ENABLED", "variants": {v: v for v in variants}, "defaultVariant": default}
flags = {
    "shield_seconds": f(["none", "short", "default", "long"], "none"),
    "player_handicap_factor": f(["default", "help"], "default"),
    "auto_rubberband": f(["off", "gentle"], "off"),
}
for key, variant in overrides.items():
    flags[key]["defaultVariant"] = variant
json.dump({"flags": flags}, open(path, "w"), indent=2)
PY
}

fixture() {
  iv_fixture
  python3 - "${AGENT_FLAG_FILE}" "$@" <<'PY'
import json, sys
path, overrides = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:])
def f(variants, default):
    return {"state": "ENABLED", "variants": variants, "defaultVariant": default}
onoff = {"on": True, "off": False}
flags = {
    "enabled": f(onoff, "on"),
    "interventions_enabled": f(onoff, "on"),
    "interventions_allowed": f({"none": "", "ambient": "play_audio_cue"}, "ambient"),
    "rollout_enabled": f(onoff, "off"),
    "experiments_enabled": f(onoff, "off"),
    "decision.backend": f({"rules": "rules", "claude": "claude"}, "rules"),
    "decision.act_confidence_threshold": f({"default": 0.8, "strict": 0.95}, "default"),
    "decision.jev.act_confidence_threshold": f({"inherit": -1.0, "gate": 0.6, "strict": 0.95}, "gate"),
    "decision.capture_content": f(onoff, "on"),
    "decision.min_interval_ms": f({"default": 3000}, "default"),
    "decision.claude.model": f({"haiku": "h", "sonnet": "s"}, "haiku"),
    "decision.claude.timeout_ms": f({"fast": 1500, "tight": 2500, "default": 5000, "patient": 8000}, "default"),
    "decision.jev.timeout_ms": f({"fast": 1500, "default": 2500, "patient": 5000}, "default"),
    "decision.jev.model": f({"latest": "jev-latest", "pinned": "jev-1.13.0"}, "latest"),
    "decision.jev.base_url": f({"default": "https://api.typesafe.ai/v1", "blackhole": "https://192.0.2.1/v1"}, "default"),
    "llm.eligible_game_kinds": f({"real_only": "real"}, "real_only"),
    "decision.openai.model": f({"unset": "", "vendor": "m"}, "unset"),
    "decision.openai.base_url": f({"default": "https://api.openai.com/v1"}, "default"),
    "llm.max_requests_per_minute": f({"default": 6, "stage": 30}, "stage"),
}
for key, variant in overrides.items():
    flags[key]["defaultVariant"] = variant
json.dump({"flags": flags}, open(path, "w"), indent=2)
PY
}

pass=0
fail=0
ok() { echo "ok   - $1"; pass=$((pass + 1)); }
not_ok() { echo "FAIL - $1"; fail=$((fail + 1)); }

# run_preflight: sets OUT and RC.
run_preflight() {
  RC=0
  OUT="$("${PREFLIGHT}" "$@" 2>&1)" || RC=$?
}
has() { [[ "${OUT}" == *"$1"* ]]; }
check() { if has "$2"; then ok "$1"; else not_ok "$1 (missing: $2)"; fi; }

# --- happy path -------------------------------------------------------------
fixture
STUB_BUDGET=30 run_preflight
if [[ "${RC}" -eq 0 ]]; then ok "baseline stack exits 0"; else not_ok "baseline stack exits 0 (rc=${RC}): ${OUT}"; fi
check "flagd mount matches" "PASS  joustmania-flagd: mounts ${WORK}/flags"
check "jev key present" "PASS  jev: JEV_API_KEY present"
check "claude key present" "PASS  claude: ANTHROPIC_API_KEY present"
check "empty openai key is only info" "INFO  openai: DECISION_OPENAI_API_KEY absent"
check "inference backend claude passes" "PASS  AGENT_INFERENCE_BACKEND=claude"
check "budget override passes" "PASS  AGENT_LLM_MAX_REQUESTS_PER_MINUTE=30"
check "claude timeout baseline" "PASS  decision.claude.timeout_ms = default (5000)"
check "jev threshold baseline" "PASS  decision.jev.act_confidence_threshold = gate (0.6)"
check "jev blackhole variant present" "PASS  decision.jev.base_url = default"
if has "stub"; then not_ok "no stub wording"; else ok "no stub wording"; fi
check "interventions neutral" "PASS  interventions.json at neutral variants"
check "jaeger newest span summarised" "newest: backend=claude outcome=held"
check "vendor variant listed" "decision.openai.model variants available: vendor"
check "clock in sync" "PASS  clock skew 0s"
if has "${SECRET}"; then not_ok "key value never printed"; else ok "key value never printed"; fi

# --- drift from the stage baseline -----------------------------------------
fixture enabled=off decision.act_confidence_threshold=strict decision.jev.act_confidence_threshold=inherit
before="$(cat "${AGENT_FLAG_FILE}")"
run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "drift exits 1"; else not_ok "drift exits 1 (rc=${RC})"; fi
check "kill switch off fails" "FAIL  enabled = off (false), stage baseline is on"
check "strict threshold fails" "FAIL  decision.act_confidence_threshold = strict (0.95), stage baseline is default"
check "jev threshold inherit fails" "FAIL  decision.jev.act_confidence_threshold = inherit (-1.0), stage baseline is gate"
check "unset env falls back to the stage flag" "PASS  LLM budget: llm.max_requests_per_minute = stage (30/min"
if [[ "$(cat "${AGENT_FLAG_FILE}")" == "${before}" ]]; then ok "flag file untouched"; else not_ok "flag file untouched"; fi

# --- budget, timeout, keys and summary backend ----------------------------
fixture llm.max_requests_per_minute=default decision.claude.timeout_ms=patient decision.jev.base_url=blackhole
STUB_JEV_KEY="" STUB_INFERENCE=stub run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "drift + missing jev key exits 1"; else not_ok "drift + missing jev key exits 1 (rc=${RC})"; fi
check "default flag budget warns" "WARN  LLM budget: llm.max_requests_per_minute = default (6/min)"
check "claude timeout drift fails" "FAIL  decision.claude.timeout_ms = patient (8000), stage baseline is default"
check "jev blackhole left on fails" "FAIL  decision.jev.base_url = blackhole"
check "missing jev key fails" "FAIL  jev: JEV_API_KEY missing"
check "stub summary backend warns" "WARN  AGENT_INFERENCE_BACKEND=stub (not claude)"
STUB_BUDGET=10 run_preflight
check "low env budget warns" "WARN  AGENT_LLM_MAX_REQUESTS_PER_MINUTE=10 replaces the flag"

# --- missing claude key; secret still never printed ------------------------
fixture
STUB_BUDGET=30 STUB_CLAUDE_KEY="" run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "missing claude key exits 1"; else not_ok "missing claude key exits 1 (rc=${RC})"; fi
check "missing claude key fails" "FAIL  claude: ANTHROPIC_API_KEY missing"
if has "${SECRET}"; then not_ok "key value never printed (missing-key run)"; else ok "key value never printed (missing-key run)"; fi

# --- blackhole variant missing ------------------------------------------------
fixture
python3 - "${AGENT_FLAG_FILE}" <<'PY2'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
del data["flags"]["decision.jev.base_url"]["variants"]["blackhole"]
json.dump(data, open(path, "w"), indent=2)
PY2
STUB_BUDGET=30 run_preflight
check "missing blackhole variant fails" "FAIL  decision.jev.base_url has no blackhole variant"

# --- expected backend override ---------------------------------------------
fixture decision.backend=claude
TALK_EXPECT_BACKEND=claude run_preflight
check "TALK_EXPECT_BACKEND honoured" "PASS  decision.backend = claude"

# --- wrong flag dir + clock skew --------------------------------------------
fixture
STUB_MOUNT=/somewhere/else STUB_SKEW=300 run_preflight
check "mount mismatch fails" "FAIL  joustmania-flagd: mounts /somewhere/else"
check "skew warns" "WARN  clock skew 300s"

# --- interventions left over from a run ------------------------------------
fixture
iv_fixture shield_seconds=default auto_rubberband=gentle
run_preflight
check "applied interventions warn" "WARN  interventions.json not neutral: shield_seconds=default, auto_rubberband=gentle"

# --- container missing -----------------------------------------------------
fixture
STUB_MISSING=joustmania-agent run_preflight
check "missing container named" "FAIL  joustmania-agent: container not found, cannot check its flag mount"

# --- usage -----------------------------------------------------------------
run_preflight --minutes abc
if [[ "${RC}" -eq 2 ]]; then ok "bad --minutes is a usage error"; else not_ok "bad --minutes is a usage error (rc=${RC})"; fi

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
