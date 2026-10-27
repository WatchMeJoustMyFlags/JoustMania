#!/usr/bin/env bash
# Self-contained tests for scripts/talk-preflight.sh (#743, #1316 UC-070 BR-006, A9).
#
# Runs the preflight against scripts/tests/fake_flag_service.py serving a fixture
# agent flag set (epic #1407), with `docker`, `curl` and `timedatectl` stubbed on
# PATH, so no stack, network or daemon is needed.
#
# Runs as part of `make lint` (needs only bash + python3 + GNU date), or directly:
#   ./scripts/tests/test-talk-preflight.sh
#
# UC: UC-070 main A9 BR-003 BR-004 BR-006 BR-007 Pre-4
# UC: UC-002 BR-003
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFLIGHT="${SCRIPT_DIR}/../talk-preflight.sh"
WORK="$(mktemp -d)"
# shellcheck source=scripts/tests/lib-fake-flag-service.sh
source "${SCRIPT_DIR}/lib-fake-flag-service.sh"
trap 'stop_fake_flag_service; rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin" "${WORK}/flags"
export FAKE_AGENT_FLAGS="${WORK}/flags/agent.json"
export FAKE_CONTROLLER_FLAGS="${WORK}/flags/controller.json"
echo '{"flags": {}}' >"${FAKE_CONTROLLER_FLAGS}"
export TALK_CLOCK_URLS="https://clock.test"
REAL_DATE="$(command -v date)"
export PATH="${WORK}/bin:${PATH}"
SECRET="sk-test-DO-NOT-PRINT-123"

# docker stub: inspect answers from STUB_* env; everything else succeeds.
cat >"${WORK}/bin/docker" <<'SH'
#!/usr/bin/env bash
[[ "$1" == "info" && -n "${STUB_DOCKER_DOWN:-}" ]] && exit 1
[[ "$1" == "info" ]] && exit 0
if [[ "$1" == "exec" ]]; then
  # docker exec joustmania-connect-proxy nc -z -w 3 host.docker.internal 8002 (#1450)
  if [[ "$2" == "joustmania-connect-proxy" ]]; then [[ -z "${STUB_PAIRING_UNREACHABLE:-}" ]]; exit; fi
  # docker exec joustmania-audio python -m services.audio.sound_catalog <cue>
  if [[ -n "${STUB_CUE_FAIL:-}" ]]; then echo "${@: -1}: not in the sound registry"; exit 1; fi
  echo "${@: -1}: Joust/sounds/beep_loud.ogg"; exit 0
fi
if [[ "$1" == "inspect" ]]; then
  [[ -n "${STUB_MISSING:-}" && "$4" == "${STUB_MISSING}" ]] && exit 1
  fmt="$3"
  # rust-hid state override (#1371): "<status> <health>", e.g. "running starting".
  [[ "$4" == "joustmania-rust-hid" && -n "${STUB_RUST_HID_STATE:-}" ]] && { echo "${STUB_RUST_HID_STATE}"; exit 0; }
  case "${fmt}" in
    *State.Status*) if [[ -n "${STUB_AUDIO_DOWN:-}" && "$4" == "joustmania-audio" ]]; then echo "exited"; else echo "running healthy"; fi ;;
    *Mounts*) echo "${STUB_MOUNT}" ;;
    *Config.Env*) printf '%s\n' "PATH=/usr/bin" "ANTHROPIC_API_KEY=${STUB_CLAUDE_KEY-${STUB_SECRET}}" "JEV_API_KEY=${STUB_JEV_KEY-${STUB_SECRET}}" "DECISION_OPENAI_API_KEY=" "AGENT_LLM_MAX_REQUESTS_PER_MINUTE=${STUB_BUDGET:-}" ;;
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
    *GetConnectedControllers*)
      [[ -n "${STUB_CM_DOWN:-}" ]] && exit 22
      echo "${STUB_CONTROLLERS:-{\"connectedSerials\":[\"00:06:f7:aa:bb:01\",\"00:06:f7:aa:bb:02\",\"MOCK0001\"]\}}"
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

# restart_fake [fake args]: serve the fixture file as the flag service's baseline.
restart_fake() {
  stop_fake_flag_service
  start_fake_flag_service "${WORK}" "$@"
}

fixture() {
  python3 - "${FAKE_AGENT_FLAGS}" "$@" <<'PY'
import json, sys
path, overrides = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:])
def f(variants, default):
    return {"state": "ENABLED", "variants": variants, "defaultVariant": default}
onoff = {"on": True, "off": False}
flags = {
    "enabled": f(onoff, "on"),
    "interventions_enabled": f(onoff, "on"),
    "interventions_allowed": f({
        "none": "",
        "probe": "noop",
        "ambient": "play_audio_cue,send_controller_effect,adjust_volume",
        "standard": "play_audio_cue,send_controller_effect,adjust_volume,adjust_music_tempo,"
                    "set_pacing_profile,adjust_player_sensitivity,grant_shield",
        "quiet": "send_controller_effect,adjust_volume",
    }, "standard"),
    "rollout_enabled": f(onoff, "off"),
    "experiments_enabled": f(onoff, "off"),
    "experiment_dynamic_enabled": f(onoff, "off"),
    "experiment_tick_seconds": f({"fast": 10, "default": 30, "slow": 60}, "default"),
    "decision.backend": f({"rules": "rules", "claude": "claude"}, "rules"),
    "decision.act_confidence_threshold": f({"default": 0.8, "strict": 0.95}, "default"),
    "decision.jev.act_confidence_threshold": f({"inherit": -1.0, "gate": 0.6, "strict": 0.95}, "gate"),
    "decision.capture_content": f(onoff, "on"),
    "decision.min_interval_ms": f({"default": 3000}, "default"),
    "decision.claude.model": f({"haiku": "h", "sonnet": "s"}, "haiku"),
    "decision.claude.timeout_ms": f({"fast": 1500, "tight": 2500, "default": 5000, "patient": 8000}, "patient"),
    "decision.jev.timeout_ms": f({"fast": 1500, "default": 2500, "patient": 5000}, "default"),
    "decision.jev.model": f({"latest": "jev-latest", "pinned": "jev-1.13.0"}, "latest"),
    "decision.jev.base_url": f({"default": "https://api.typesafe.ai/v1", "blackhole": "https://192.0.2.1/v1"}, "default"),
    "llm.eligible_game_kinds": f({"none": "", "real_only": "real", "real_and_shadow": "real,shadow"}, "real_only"),
    "experiment.mock_archetypes": f({"all": "calm_long,slow_cascade,quick_cascade", "quick_cascade": "quick_cascade"}, "all"),
    "decision.openai.model": f({"unset": "", "vendor": "m"}, "unset"),
    "decision.openai.base_url": f({"default": "https://api.openai.com/v1"}, "default"),
    "llm.max_requests_per_minute": f({"default": 6, "stage": 30}, "stage"),
    "inference.backend": f({"stub": "stub", "claude": "claude"}, "claude"),
    "policy.max_interventions_per_minute": f({"conservative": 1, "default": 2, "aggressive": 4}, "aggressive"),
}
for key, variant in overrides.items():
    flags[key]["defaultVariant"] = variant
json.dump({"flags": flags}, open(path, "w"), indent=2)
PY
  restart_fake
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
check "flagd connected to the flag service" "PASS  flagd holds 1 sync stream(s) to the flag service (epoch fake0001)"
check "agent set served" "PASS  the agent flag set is served by the flag service"
check "jev key present" "PASS  jev: JEV_API_KEY present"
check "claude key present" "PASS  claude: ANTHROPIC_API_KEY present"
check "empty openai key is only info" "INFO  openai: DECISION_OPENAI_API_KEY absent"
check "inference backend flag claude passes" "PASS  inference.backend = claude"
check "inference backend effective flag claude passes" "PASS  inference.backend flag = claude (the post-game summary backend)"
check "stage flag under the env ceiling passes" "PASS  LLM budget: llm.max_requests_per_minute = stage (30/min) under the AGENT_LLM_MAX_REQUESTS_PER_MINUTE=30 ceiling"
check "claude timeout baseline" "PASS  decision.claude.timeout_ms = patient (8000)"
check "jev threshold baseline" "PASS  decision.jev.act_confidence_threshold = gate (0.6)"
check "jev blackhole variant present" "PASS  decision.jev.base_url = default"
if has "stub"; then not_ok "no stub wording"; else ok "no stub wording"; fi
check "interventions are per game" "PASS  interventions are per game: 0 value(s) in 0 live game(s)"
check "jaeger newest span summarised" "newest: backend=claude outcome=held"
check "vendor variant listed" "decision.openai.model variants available: vendor"
check "clock in sync" "PASS  clock skew 0s"
check "stage allow-list is standard" "PASS  interventions_allowed = standard"
check "perceptible interventions listed" "PASS  perceptible interventions allowed: play_audio_cue, adjust_music_tempo, grant_shield"
check "audio cue resolves in the audio container" "PASS  audio cue agent_cue: Joust/sounds/beep_loud.ogg"
check "pairing daemon reachable from the connect-proxy" "PASS  connect-proxy reaches the pairing daemon at host.docker.internal:8002"

# --- pairing daemon unreachable from the connect-proxy is a WARN (UC-005, #1450)
fixture
STUB_BUDGET=30 STUB_PAIRING_UNREACHABLE=1 run_preflight
if [[ "${RC}" -eq 0 ]]; then ok "unreachable pairing daemon only warns (exit 0)"; else not_ok "unreachable pairing daemon only warns (rc=${RC})"; fi
check "unreachable pairing daemon warns" "WARN  connect-proxy cannot reach host.docker.internal:8002: every dashboard unpair will answer 502"
STUB_BUDGET=30 STUB_MISSING=joustmania-connect-proxy run_preflight
check "missing connect-proxy skips the reachability check" "WARN  pairing daemon reachability not checked: joustmania-connect-proxy not found"
if has "${SECRET}"; then not_ok "key value never printed"; else ok "key value never printed"; fi

# --- drift from the stage baseline -----------------------------------------
fixture enabled=off decision.act_confidence_threshold=strict decision.jev.act_confidence_threshold=inherit policy.max_interventions_per_minute=default
run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "drift exits 1"; else not_ok "drift exits 1 (rc=${RC})"; fi
check "kill switch off fails" "FAIL  enabled = off (false), stage baseline is on"
check "strict threshold fails" "FAIL  decision.act_confidence_threshold = strict (0.95), stage baseline is default"
check "jev threshold inherit fails" "FAIL  decision.jev.act_confidence_threshold = inherit (-1.0), stage baseline is gate"
check "shipped intervention budget fails (#1326)" "FAIL  policy.max_interventions_per_minute = default (2), stage baseline is aggressive"
check "unset env falls back to the stage flag" "PASS  LLM budget: llm.max_requests_per_minute = stage (30/min"
if grep -qE '^(PUT|POST|DELETE) ' "${FAKE_FLAG_LOG}"; then not_ok "the pre-flight writes nothing"; else ok "the pre-flight writes nothing"; fi

# --- budget, timeout, keys and summary backend ----------------------------
fixture llm.max_requests_per_minute=default decision.claude.timeout_ms=default decision.jev.base_url=blackhole
STUB_JEV_KEY="" run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "drift + missing jev key exits 1"; else not_ok "drift + missing jev key exits 1 (rc=${RC})"; fi
check "default flag budget warns" "WARN  LLM budget: llm.max_requests_per_minute = default (6/min)"
check "claude timeout drift fails" "FAIL  decision.claude.timeout_ms = default (5000), stage baseline is patient"
check "jev blackhole left on fails" "FAIL  decision.jev.base_url = blackhole"
check "missing jev key fails" "FAIL  jev: JEV_API_KEY missing"
STUB_BUDGET=10 run_preflight
check "low env budget warns" "WARN  AGENT_LLM_MAX_REQUESTS_PER_MINUTE=10 caps the flag"
# #1788: the env is a ceiling, so 30 in the env cannot raise the default flag (6).
STUB_BUDGET=30 run_preflight
check "env ceiling cannot raise the default flag" "WARN  LLM budget: llm.max_requests_per_minute = default (6/min)"

# --- the inference.backend flag is the effective choice (#1810, #1907) --------
# Flag at its shipped stub default: the closing summary beat would be capture-only.
# Both the baseline check and the section-4 effective-flag check FAIL.
fixture inference.backend=stub
STUB_BUDGET=30 run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "inference.backend stub exits 1"; else not_ok "inference.backend stub exits 1 (rc=${RC})"; fi
check "inference.backend stub drift fails" "FAIL  inference.backend = stub (\"stub\"), stage baseline is claude"
check "inference.backend stub: effective-flag check fails" "FAIL  inference.backend flag = stub (not claude)"
# Flag claude: the effective flag passes and no env var is consulted (#1907).
fixture
STUB_BUDGET=30 run_preflight
if [[ "${RC}" -eq 0 ]]; then ok "flag claude exits 0"; else not_ok "flag claude exits 0 (rc=${RC})"; fi
check "flag claude: flag passes" "PASS  inference.backend = claude"
if has "AGENT_INFERENCE_BACKEND"; then not_ok "the removed env var is not mentioned"; else ok "the removed env var is not mentioned"; fi

# --- missing claude key; secret still never printed ------------------------
fixture
STUB_BUDGET=30 STUB_CLAUDE_KEY="" run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "missing claude key exits 1"; else not_ok "missing claude key exits 1 (rc=${RC})"; fi
check "missing claude key fails" "FAIL  claude: ANTHROPIC_API_KEY missing"
if has "${SECRET}"; then not_ok "key value never printed (missing-key run)"; else ok "key value never printed (missing-key run)"; fi

# --- blackhole variant missing ------------------------------------------------
fixture
python3 - "${FAKE_AGENT_FLAGS}" <<'PY2'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
del data["flags"]["decision.jev.base_url"]["variants"]["blackhole"]
json.dump(data, open(path, "w"), indent=2)
PY2
restart_fake
STUB_BUDGET=30 run_preflight
check "missing blackhole variant fails" "FAIL  decision.jev.base_url has no blackhole variant"

# --- beat variants missing ----------------------------------------------------
fixture
python3 - "${FAKE_AGENT_FLAGS}" <<'PY3'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
del data["flags"]["decision.jev.act_confidence_threshold"]["variants"]["strict"]
del data["flags"]["decision.claude.timeout_ms"]["variants"]["patient"]
json.dump(data, open(path, "w"), indent=2)
PY3
restart_fake
STUB_BUDGET=30 run_preflight
check "missing strict threshold variant fails" "FAIL  decision.jev.act_confidence_threshold has no strict variant"
check "missing patient timeout variant warns" "WARN  decision.claude.timeout_ms has no patient variant"
fixture
STUB_BUDGET=30 run_preflight
check "beat variants present pass" "PASS  decision.jev.act_confidence_threshold has the strict and gate variants"

# --- expected backend override ---------------------------------------------
fixture decision.backend=claude
TALK_EXPECT_BACKEND=claude run_preflight
check "TALK_EXPECT_BACKEND honoured" "PASS  decision.backend = claude"

if has "allows no intervention"; then not_ok "remote backend + ambient does not warn"; else ok "remote backend + ambient does not warn"; fi

# --- remote backend with nothing allowed (#1298) ----------------------------
fixture decision.backend=claude interventions_allowed=none
TALK_EXPECT_BACKEND=claude STUB_BUDGET=30 run_preflight
check "remote backend + none warns" "WARN  decision.backend = claude but interventions_allowed = none (\"\") allows no intervention"
fixture decision.backend=claude interventions_allowed=probe
TALK_EXPECT_BACKEND=claude STUB_BUDGET=30 run_preflight
check "remote backend + probe (noop only) warns" "WARN  decision.backend = claude but interventions_allowed = probe"
fixture interventions_allowed=none
STUB_BUDGET=30 run_preflight
if has "allows no intervention"; then not_ok "rules backend + none does not warn"; else ok "rules backend + none does not warn"; fi

# --- a persisted remote backend comes back after a restart (#1682) ---------
# persist_put <flag> <variant> [persist]: a write to the fake (curl is stubbed).
persist_put() {
  python3 - "${FLAG_SERVICE_URL}/agent/$1" "$2" "${3:-true}" <<'PY2'
import json, sys, urllib.request
body = {"variant": sys.argv[2], "writer": "stage"}
if sys.argv[3] == "true":
    body["persist"] = True
req = urllib.request.Request(sys.argv[1], json.dumps(body).encode(), {"Content-Type": "application/json"}, method="PUT")
urllib.request.urlopen(req).read()
PY2
}
fixture
persist_put decision.backend claude
persist_put decision.claude.model sonnet
persist_put decision.backend rules false
STUB_BUDGET=30 run_preflight
check "stage overlay rules over a persisted claude still passes the effective check" "PASS  decision.backend = rules"
check "persisted remote backend warns" "WARN  decision.backend claude is persisted with decision.claude.model = sonnet: a flag-service restart or a reboot brings claude back"
fixture
persist_put decision.backend rules
STUB_BUDGET=30 run_preflight
if has "is persisted with\|brings rules back"; then not_ok "persisted rules does not warn"; else ok "persisted rules does not warn"; fi
check "persisted rules is reported" "INFO  decision.backend rules is persisted"
fixture
STUB_BUDGET=30 run_preflight
if has "is persisted"; then not_ok "no persisted backend, no persisted line"; else ok "no persisted backend, no persisted line"; fi

# --- clock skew ----------------------------------------------------------------
fixture
STUB_SKEW=300 run_preflight
check "skew warns" "WARN  clock skew 300s"

# --- live games hold interventions (they die with their game) --------------
fixture
restart_fake --games 1 --scope-values 3
run_preflight
check "live game values reported" "PASS  interventions are per game: 3 value(s) in 1 live game(s)"

# --- the flag service down or flagd disconnected --------------------------------
fixture
stop_fake_flag_service
STUB_BUDGET=30 run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "flag service down exits 1"; else not_ok "flag service down exits 1 (rc=${RC})"; fi
check "flag service down named" "FAIL  flag service unreachable"
check "agent flags unreadable named" "FAIL  cannot read the agent flags from the flag service"

# --- container missing -----------------------------------------------------
fixture
STUB_MISSING=joustmania-flag-service run_preflight
check "missing flag-service container named" "FAIL  joustmania-flag-service: not found"

# --- controller backend: rust-hid up when backend=rust (#1371, UC-002 BR-003)
controller_fixture() {
  python3 - "${FAKE_CONTROLLER_FLAGS}" "$1" <<'PY'
import json, sys
variants = {"python": "python", "mock": "mock", "rust": "rust", "python_rust": "python,rust"}
json.dump({"flags": {"backend": {"state": "ENABLED", "variants": variants, "defaultVariant": sys.argv[2]}}},
          open(sys.argv[1], "w"), indent=2)
PY
  restart_fake
}
fixture
STUB_BUDGET=30 run_preflight
check "no backend flag skips the rust-hid check" "WARN  cannot read the controller backend flag from the flag service"

controller_fixture rust
STUB_BUDGET=30 run_preflight
if [[ "${RC}" -eq 0 ]]; then ok "backend=rust + healthy rust-hid exits 0"; else not_ok "backend=rust + healthy rust-hid exits 0 (rc=${RC})"; fi
check "backend=rust + healthy rust-hid passes" "PASS  backend = rust; joustmania-rust-hid running (healthy)"

controller_fixture python_rust
STUB_BUDGET=30 STUB_RUST_HID_STATE="running starting" run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "unhealthy rust-hid exits 1"; else not_ok "unhealthy rust-hid exits 1 (rc=${RC})"; fi
check "comma list naming rust checks rust-hid health" "FAIL  backend = python,rust but joustmania-rust-hid is not healthy (health: starting)"

controller_fixture rust
STUB_BUDGET=30 STUB_MISSING=joustmania-rust-hid run_preflight
check "missing rust-hid named" "FAIL  backend = rust but joustmania-rust-hid is not running"

controller_fixture rust
STUB_BUDGET=30 STUB_DOCKER_DOWN=1 run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "docker down still exits 1 (named once)"; else not_ok "docker down still exits 1 (rc=${RC})"; fi
check "docker down degrades the rust-hid check to a skip" "INFO  backend = rust: rust-hid check skipped (docker unavailable"

controller_fixture mock
STUB_BUDGET=30 STUB_MISSING=joustmania-rust-hid run_preflight
check "backend=mock does not need rust-hid" "INFO  backend = mock: rust-hid not needed"
if has "joustmania-rust-hid is not running"; then not_ok "backend=mock never fails on rust-hid"; else ok "backend=mock never fails on rust-hid"; fi
echo '{"flags": {}}' >"${FAKE_CONTROLLER_FLAGS}"
restart_fake

# --- stage permission baseline and perceptibility (UC-070 BR-003/BR-004, #1301)
fixture interventions_allowed=none
STUB_BUDGET=30 run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "allow-list none exits 1"; else not_ok "allow-list none exits 1 (rc=${RC})"; fi
check "allow-list none fails loudly" "FAIL  interventions_allowed = none (\"\"): nothing can act on stage"
check "allow-list none names the reset" "reset: ./scripts/talk-stage.sh reset"
check "none allows nothing perceptible" "FAIL  interventions_allowed = none allows nothing the audience can perceive (nothing)"

fixture interventions_allowed=probe
STUB_BUDGET=30 run_preflight
check "allow-list probe fails loudly" "FAIL  interventions_allowed = probe (\"noop\"): nothing can act on stage"

fixture interventions_allowed=ambient
STUB_BUDGET=30 run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "ambient is off the stage baseline"; else not_ok "ambient is off the stage baseline (rc=${RC})"; fi
check "ambient fails the baseline" "stage baseline is standard (reset: ./scripts/talk-stage.sh reset)"
check "ambient still has the audible cue" "PASS  perceptible interventions allowed: play_audio_cue"

fixture interventions_allowed=quiet
STUB_BUDGET=30 run_preflight
check "no perceptible intervention fails" "FAIL  interventions_allowed = quiet allows nothing the audience can perceive (send_controller_effect, adjust_volume)"

fixture
STUB_BUDGET=30 STUB_CUE_FAIL=1 run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "unresolved audio cue exits 1"; else not_ok "unresolved audio cue exits 1 (rc=${RC})"; fi
check "unresolved audio cue fails" "FAIL  audio cue 'agent_cue' does not resolve in joustmania-audio"
if has "audio container not running"; then not_ok "running container is not reported as down"; else ok "running container is not reported as down"; fi

fixture
STUB_BUDGET=30 STUB_AUDIO_DOWN=1 run_preflight
check "stopped audio container is named, not an old image" "FAIL  audio cue 'agent_cue' not checked: audio container not running (joustmania-audio: exited)"
if has "older than #1301"; then not_ok "stopped container does not blame the image"; else ok "stopped container does not blame the image"; fi

# --- real controllers paired (UC-070 BR-006, #1316) -------------------------
fixture
STUB_BUDGET=30 run_preflight
check "two real controllers pass, the mock is not counted" "PASS  2 real controller(s) connected (need 2): 00:06:f7:aa:bb:01 00:06:f7:aa:bb:02"

fixture
STUB_BUDGET=30 STUB_CONTROLLERS='{"connectedSerials":["00:06:f7:aa:bb:01","MOCK0001","mock_controller_0"]}' run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "one real controller exits 1"; else not_ok "one real controller exits 1 (rc=${RC})"; fi
check "too few real controllers fail (mocks not counted)" "FAIL  1 real controller(s) connected, need 2 (2 mock not counted)"
check "the failure names the fallback" "talk-stage.sh fallback on"

fixture
STUB_BUDGET=30 TALK_MIN_CONTROLLERS=1 STUB_CONTROLLERS='{"connectedSerials":["00:06:f7:aa:bb:01"]}' run_preflight
check "TALK_MIN_CONTROLLERS honoured" "PASS  1 real controller(s) connected (need 1)"
STUB_BUDGET=30 TALK_MIN_CONTROLLERS=4 run_preflight
check "TALK_MIN_CONTROLLERS raises the bar" "FAIL  2 real controller(s) connected, need 4"
STUB_BUDGET=30 TALK_MIN_CONTROLLERS=two run_preflight
check "non-numeric TALK_MIN_CONTROLLERS fails" "FAIL  TALK_MIN_CONTROLLERS=two is not a number"

fixture
STUB_BUDGET=30 STUB_CONTROLLERS='{}' run_preflight
check "an empty roster fails" "FAIL  0 real controller(s) connected, need 2"

fixture
STUB_BUDGET=30 STUB_CM_DOWN=1 run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "controller-manager down exits 1"; else not_ok "controller-manager down exits 1 (rc=${RC})"; fi
check "controller-manager down degrades to a named FAIL" "FAIL  controller-manager not reachable at http://localhost (GetConnectedControllers)"
check "controller-manager down names the containers, not a pairing fix" "check the containers section; the fallback needs controller-manager as well"
if grep -q "FAIL  controller-manager not reachable.*fallback on" <<<"${OUT}"; then not_ok "controller-manager down does not suggest fallback on"; else ok "controller-manager down does not suggest fallback on"; fi

# --- screen-only fallback on (UC-070 A9, BR-006, BR-007) --------------------
fixture llm.eligible_game_kinds=real_and_shadow experiments_enabled=on experiment_dynamic_enabled=on experiment_tick_seconds=fast
STUB_BUDGET=30 STUB_CONTROLLERS='{"connectedSerials":["MOCK0001"]}' run_preflight
if [[ "${RC}" -eq 0 ]]; then ok "fallback on + no real controllers exits 0"; else not_ok "fallback on + no real controllers exits 0 (rc=${RC}): $(grep FAIL <<<"${OUT}")"; fi
check "fallback on is announced" "WARN  screen-only fallback is ON"
check "fallback names its cost bound" "cost bounded by llm.max_requests_per_minute"
check "fallback variant accepted" "PASS  llm.eligible_game_kinds = real_and_shadow"
check "fallback experiment loop accepted" "PASS  experiments_enabled = on"
check "fallback dynamic declarer accepted" "PASS  experiment_dynamic_enabled = on"
check "fallback fast tick accepted" "PASS  experiment_tick_seconds = fast"
check "too few real controllers only warn in fallback" "WARN  0 real controller(s) connected, need 2 (1 mock not counted) (fallback on"
check "calm_long in the selection" "PASS  experiment.mock_archetypes = all includes calm_long"
STUB_BUDGET=30 STUB_CM_DOWN=1 run_preflight
if [[ "${RC}" -eq 0 ]]; then ok "fallback on + controller-manager down exits 0"; else not_ok "fallback on + controller-manager down exits 0 (rc=${RC})"; fi
check "controller-manager down only warns in fallback" "WARN  controller-manager not reachable"
check "fallback warning says the shadow games need controller-manager" "the fallback's shadow games need controller-manager too"

fixture llm.eligible_game_kinds=real_and_shadow experiments_enabled=on experiment_dynamic_enabled=on experiment_tick_seconds=fast experiment.mock_archetypes=quick_cascade
STUB_BUDGET=30 run_preflight
check "fallback without calm_long warns" "WARN  experiment.mock_archetypes = quick_cascade (quick_cascade) has no calm_long"

fixture llm.eligible_game_kinds=real_and_shadow
STUB_BUDGET=30 run_preflight
if [[ "${RC}" -eq 1 ]]; then ok "half-applied fallback exits 1"; else not_ok "half-applied fallback exits 1 (rc=${RC})"; fi
check "half-applied fallback names the command" "FAIL  experiments_enabled = off (false), stage baseline is on (fallback: ./scripts/talk-stage.sh fallback on"

fixture experiments_enabled=on
STUB_BUDGET=30 run_preflight
check "experiments on without the fallback fails the stage baseline" "FAIL  experiments_enabled = on (true), stage baseline is off (reset: ./scripts/talk-stage.sh reset)"
if has "screen-only fallback is ON"; then not_ok "no fallback without real_and_shadow"; else ok "no fallback without real_and_shadow"; fi

# --- usage -----------------------------------------------------------------
run_preflight --minutes abc
if [[ "${RC}" -eq 2 ]]; then ok "bad --minutes is a usage error"; else not_ok "bad --minutes is a usage error (rc=${RC})"; fi

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
