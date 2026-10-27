#!/usr/bin/env bash
# Read-only pre-flight for the "Two Standards Walk Into an Agent" talk (#743,
# epic #1225). Runbook: docs/talk/two-standards-runbook.md.
#
# Prints one PASS / WARN / FAIL / INFO line per check and exits non-zero when
# any check FAILs. It changes NOTHING: it reads the flag service (GET /state,
# GET /agent; epic #1407), inspects containers, and sends GET/HEAD requests.
#
# Checks:
#   1. the stack containers are running (and healthy where they have a check)
#   1b. the controller backend: when the `backend` flag of the controller set (read
#      from the flag service) names rust (the default, #1371), the rust-hid
#      container is running and healthy (UC-002 BR-003)
#   1c. the pairing daemon's control port (host.docker.internal:8002) is
#      reachable from the connect-proxy container (TCP connect only); WARN
#      otherwise: every dashboard unpair answers 502 (UC-005 A7, BR-003, #1450)
#   2. the flag service: flagd holds an open sync stream (else every write is
#      refused with 503 no_sync_stream), the boot epoch, the agent set served
#   3. the stage baseline of the decision.* + safety flags (the effective variant
#      the flag service serves, from scripts/talk-baseline.json;
#      interventions_allowed=standard, #1301), at least one perceptible
#      intervention in the resolved allow-list, and how many game-scoped
#      intervention values are live (they die with their game); WARN
#      when decision.backend is remote but interventions_allowed resolves to
#      nothing (none / noop only): the agent then makes no remote call (#1298);
#      WARN when a remote decision.backend (jev, claude, openai) is PERSISTED
#      (agent-backend.sh, #1682): a stage overlay over it is temporary, and a
#      flag-service restart or a reboot brings the persisted backend back
#   4. the agent container env (presence only; key values are never printed):
#      JEV_API_KEY (Jev, the main in-game backend) and ANTHROPIC_API_KEY (Claude:
#      in-game backup + post-game summary) present, the effective inference.backend
#      flag is claude (FAIL otherwise; also checked as a baseline value in 3),
#      and the shared LLM budget >= 30/min (the
#      flag's value, capped by the AGENT_LLM_MAX_REQUESTS_PER_MINUTE ceiling, #1788)
#   4b. the agent's audio cue (defaultAudioCue in services/agent/actions/writer.go)
#      resolves to a file in the RUNNING audio container (UC-070 BR-004)
#   5. Jaeger reachable and holding agent.decide spans from the last N minutes
#   6. the dashboard reachable through envoy
#   7. the clock: local time vs the HTTP Date header of a reachable host
#   8. real PS Move controllers: at least TALK_MIN_CONTROLLERS non-mock
#      controllers connected, as controller-manager reports them through envoy
#      (UC-070 BR-006). FAIL, or WARN while the screen-only fallback is on
#      (llm.eligible_game_kinds=real_and_shadow, `talk-stage.sh fallback on`,
#      UC-070 A9); controller-manager unreachable FAILs (WARN in fallback) without
#      suggesting the fallback, which needs it too; with the fallback on, section 3 expects the baseline's
#      `fallback` variants and a calm_long archetype in the selection
#
# Usage:
#   ./scripts/talk-preflight.sh                  # all checks
#   ./scripts/talk-preflight.sh --minutes 30     # wider Jaeger window
#
# Environment:
#   FLAG_SERVICE_URL     the flag service (default $TALK_BASE_URL/flags, the envoy route)
#   TALK_BASE_URL        envoy entry point (default http://localhost)
#   TALK_EXPECT_BACKEND  expected decision.backend variant (default rules: the
#                        session starts on the floor, beat 2 flips to jev)
#   TALK_BASELINE_FILE   stage baseline (default scripts/talk-baseline.json)
#   TALK_CLOCK_URLS      space-separated URLs whose Date header is the time
#                        reference (default: api.anthropic.com, cloudflare, google)
#   TALK_MAX_SKEW        allowed clock skew in seconds (default 60)
#   TALK_MIN_CONTROLLERS real PS Move controllers needed on stage (default 2)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BASE_URL="${TALK_BASE_URL:-http://localhost}"
BASE_URL="${BASE_URL%/}"
export FLAG_SERVICE_URL="${FLAG_SERVICE_URL:-${BASE_URL}/flags}"
EXPECT_BACKEND="${TALK_EXPECT_BACKEND:-rules}"
BASELINE_FILE="${TALK_BASELINE_FILE:-${SCRIPT_DIR}/talk-baseline.json}"
WRITER_GO="${REPO_ROOT}/services/agent/actions/writer.go"
CLOCK_URLS="${TALK_CLOCK_URLS:-https://api.anthropic.com https://www.cloudflare.com https://www.google.com}"
MAX_SKEW="${TALK_MAX_SKEW:-60}"
MIN_CONTROLLERS="${TALK_MIN_CONTROLLERS:-2}"
MINUTES=15

usage() {
  echo "usage: $0 [--minutes N]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minutes)
      [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || usage
      MINUTES="$2"
      shift 2
      ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

n_pass=0
n_warn=0
n_fail=0

pass() { echo "PASS  $*"; n_pass=$((n_pass + 1)); }
warn() { echo "WARN  $*"; n_warn=$((n_warn + 1)); }
fail() { echo "FAIL  $*"; n_fail=$((n_fail + 1)); }
info() { echo "INFO  $*"; }
section() { echo; echo "== $* =="; }

have() { command -v "$1" >/dev/null 2>&1; }

# --- 1. containers ---------------------------------------------------------
section "stack containers"
CONTAINERS=(
  joustmania-flag-service joustmania-flagd joustmania-agent joustmania-otel-collector joustmania-jaeger
  joustmania-game-coordinator joustmania-controller-manager joustmania-menu
  joustmania-audio joustmania-dashboard joustmania-envoy
)
docker_ok=0
if ! have docker; then
  fail "docker CLI not found; container, mount and key checks skipped"
elif ! docker info >/dev/null 2>&1; then
  fail "docker daemon not reachable; container, mount and key checks skipped"
else
  docker_ok=1
  for name in "${CONTAINERS[@]}"; do
    state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "${name}" 2>/dev/null)" || state=""
    read -r status health <<<"${state}"
    if [[ -z "${state}" ]]; then
      fail "${name}: not found (stack not up, or wrong compose profile)"
    elif [[ "${status}" != "running" ]]; then
      fail "${name}: ${status}"
    elif [[ -n "${health:-}" && "${health}" != "healthy" ]]; then
      fail "${name}: running but ${health}"
    else
      pass "${name}: running${health:+ (${health})}"
    fi
  done
fi

# --- 1b. controller backend: rust-hid up when backend=rust (#1371) --------
section "controller backend (rust-hid when backend=rust)"
backend_value="$(PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 -c '
from flagservice import FlagService
try:
    print(FlagService.from_env().effective("controller")["backend"][1])
except Exception:
    print("?")
' 2>/dev/null)"
if [[ -z "${backend_value}" || "${backend_value}" == "?" ]]; then
  warn "cannot read the controller backend flag from the flag service (${FLAG_SERVICE_URL}); rust-hid check skipped"
elif [[ ",${backend_value// /}," != *",rust,"* ]]; then
  info "backend = ${backend_value}: rust-hid not needed"
elif [[ "${docker_ok}" -ne 1 ]]; then
  info "backend = ${backend_value}: rust-hid check skipped (docker unavailable, see above)"
else
  rust_state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' joustmania-rust-hid 2>/dev/null)" || rust_state=""
  read -r rust_status rust_health <<<"${rust_state}"
  if [[ -z "${rust_state}" ]]; then
    fail "backend = ${backend_value} but joustmania-rust-hid is not running: no real controller can connect (start the stack that ships rust-hid: docker-compose.yml or docker-compose.lite.yml)"
  elif [[ "${rust_status}" != "running" ]]; then
    fail "backend = ${backend_value} but joustmania-rust-hid is ${rust_status}"
  elif [[ "${rust_health:-}" != "healthy" ]]; then
    fail "backend = ${backend_value} but joustmania-rust-hid is not healthy (health: ${rust_health:-none})"
  else
    pass "backend = ${backend_value}; joustmania-rust-hid running (healthy)"
  fi
fi

# --- 1c. pairing daemon reachable from the connect-proxy (UC-005, #1450) ---
section "pairing daemon control port (from the connect-proxy)"
if [[ "${docker_ok}" -ne 1 ]]; then
  info "pairing daemon reachability not checked (docker unavailable, see above)"
else
  proxy_state="$(docker inspect -f '{{.State.Status}}' joustmania-connect-proxy 2>/dev/null)" || proxy_state=""
  proxy_state="${proxy_state%% *}"
  if [[ "${proxy_state}" != "running" ]]; then
    warn "pairing daemon reachability not checked: joustmania-connect-proxy ${proxy_state:-not found} (dashboard unpair unavailable)"
  elif docker exec joustmania-connect-proxy nc -z -w 3 host.docker.internal 8002 >/dev/null 2>&1; then
    pass "connect-proxy reaches the pairing daemon at host.docker.internal:8002"
  else
    warn "connect-proxy cannot reach host.docker.internal:8002: every dashboard unpair will answer 502 (pairing daemon down, CONTROL_BIND address not bound yet (see control_bind in curl -s 127.0.0.1:8002/healthz), or a host firewall)"
  fi
fi

# --- 2. the flag service: flagd connected, epoch, agent set served ---------
section "flag service (${FLAG_SERVICE_URL})"
svc_out="$(PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 - <<'PY'
import sys

from flagservice import FlagService, FlagServiceError, FlagServiceUnreachable

try:
    state = FlagService.from_env().state()
except (FlagServiceUnreachable, FlagServiceError) as exc:
    print(f"FAIL  {exc}: no flag can change and the stage helpers fail (start the stack; scripts/flags.sh state)")
    sys.exit(0)
streams = state.get("sync_streams", 0)
if streams >= 1:
    print(f"PASS  flagd holds {streams} sync stream(s) to the flag service (epoch {state.get('epoch')})")
else:
    print("FAIL  flagd has no sync stream: every flag write is refused (503 no_sync_stream); "
          "restart both: ./scripts/flags.sh restart")
if "agent" in state.get("served_sets", []):
    print("PASS  the agent flag set is served by the flag service")
else:
    print(f"FAIL  the agent flag set is not served (served: {', '.join(state.get('served_sets', []))})")
PY
)"
while IFS= read -r line; do
  [[ -z "${line}" ]] && continue
  case "${line}" in
    PASS*) echo "${line}"; n_pass=$((n_pass + 1)) ;;
    FAIL*) echo "${line}"; n_fail=$((n_fail + 1)) ;;
    *) echo "${line}" ;;
  esac
done <<<"${svc_out}"

# --- 3. stage baseline flags -----------------------------------------------
section "stage baseline flags (agent set, as the flag service serves it)"
if true; then
  # The python prints the result lines itself (PASS/WARN/FAIL prefix); count them.
  flag_out="$(PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 - "${EXPECT_BACKEND}" "${BASELINE_FILE}" <<'PY'
import json
import sys

from flagservice import FlagService, FlagServiceError, FlagServiceUnreachable

expect_backend, baseline_path = sys.argv[1], sys.argv[2]
fs = FlagService.from_env()
try:
    # The flags in the shape the checks below read: the variant the flag service
    # serves now (overlays included) as the defaultVariant.
    views = {view["flag"]: view for view in fs.flag_set("agent")["flags"]}
    flags = {
        key: {"state": "ENABLED", "defaultVariant": view["effective_default"],
              "variants": view.get("variant_values", {})}
        for key, view in views.items()
    }
    service_state = fs.state()
except (FlagServiceUnreachable, FlagServiceError) as exc:
    print(f"FAIL  cannot read the agent flags from the flag service: {exc}")
    sys.exit(0)

# flag key -> expected defaultVariant for the start of the talk: the stage
# baseline shared with talk-stage.sh (UC-070 BR-003). Jev's own bar (#1287) is
# the calibrated gate 0.6; the Claude timeout default 5000 ms covers Haiku.
try:
    with open(baseline_path) as fh:
        stage = json.load(fh)
except (OSError, ValueError) as exc:
    print(f"FAIL  cannot read the stage baseline {baseline_path}: {exc}")
    sys.exit(0)
BASELINE = dict(stage["checked"])
BASELINE["decision.backend"] = expect_backend
PERCEPTIBLE = stage.get("perceptible_interventions", [])
RESET_HINT = "reset: ./scripts/talk-stage.sh reset"

# Screen-only fallback (UC-070 A9, BR-006, #1316): while llm.eligible_game_kinds
# is at its fallback variant, the fallback section replaces the stage baseline
# of its flags, so a half-applied fallback still FAILs.
FALLBACK = stage.get("fallback", {})
FALLBACK_KEY = "llm.eligible_game_kinds"
fallback_on = bool(FALLBACK) and (flags.get(FALLBACK_KEY) or {}).get("defaultVariant") == FALLBACK.get(FALLBACK_KEY)
if fallback_on:
    BASELINE.update(FALLBACK)
    RESET_HINT = "fallback: ./scripts/talk-stage.sh fallback on; leave it: fallback off"
    print("WARN  screen-only fallback is ON (talk-stage.sh fallback on): shadow games stand in for real "
          "players and remote backends decide on them too (cost bounded by llm.max_requests_per_minute); "
          "turn it off with ./scripts/talk-stage.sh fallback off")
    want_archetype = stage.get("fallback_archetype", "calm_long")
    selection = flags.get("experiment.mock_archetypes") or {}
    names = str(selection.get("variants", {}).get(selection.get("defaultVariant")) or "")
    if want_archetype in [n.strip() for n in names.split(",")]:
        print(f"PASS  experiment.mock_archetypes = {selection.get('defaultVariant')} includes {want_archetype} "
              "(a shadow game runs to the cap)")
    else:
        print(f"WARN  experiment.mock_archetypes = {selection.get('defaultVariant')} ({names or 'nothing'}) has no "
              f"{want_archetype}: every shadow game may end and leave the screen empty "
              "(./scripts/talk-stage.sh reset restores 'all', then fallback on)")


def shown(flag):
    variant = flag.get("defaultVariant")
    return f"{variant} ({json.dumps(flag.get('variants', {}).get(variant))})"


for key, want in BASELINE.items():
    flag = flags.get(key)
    if flag is None:
        print(f"FAIL  {key}: not defined in the agent flag set")
    elif flag.get("state") != "ENABLED":
        print(f"FAIL  {key}: state {flag.get('state')}")
    elif flag.get("defaultVariant") == want:
        print(f"PASS  {key} = {shown(flag)}")
    elif key == "interventions_allowed" and flag.get("defaultVariant") in ("none", "probe"):
        print(f"FAIL  {key} = {shown(flag)}: nothing can act on stage (left behind by an interrupted "
              f"permissions beat?), stage baseline is {want} ({RESET_HINT})")
    else:
        print(f"FAIL  {key} = {shown(flag)}, stage baseline is {want} ({RESET_HINT})")

# The audience must be able to perceive an act (UC-070 BR-004, #1301): the
# resolved allow-list holds at least one perceptible intervention.
allowed_flag = flags.get("interventions_allowed")
if allowed_flag is not None:
    resolved = allowed_flag.get("variants", {}).get(allowed_flag.get("defaultVariant")) or ""
    allowed = [t.strip() for t in str(resolved).split(",") if t.strip()]
    perceptible = [t for t in allowed if t in PERCEPTIBLE]
    if perceptible:
        print(f"PASS  perceptible interventions allowed: {', '.join(perceptible)}")
    else:
        print(f"FAIL  interventions_allowed = {allowed_flag.get('defaultVariant')} allows nothing the audience "
              f"can perceive ({', '.join(allowed) or 'nothing'}); needs one of {', '.join(PERCEPTIBLE)} "
              f"({RESET_HINT})")


def variant_value(key):
    flag = flags.get(key) or {}
    return flag.get("variants", {}).get(flag.get("defaultVariant"))


# A remote backend with nothing allowed never calls out (#1298: the agent skips
# the call when noop would be the only option), so the demo would show nothing.
backend_value = variant_value("decision.backend")
allowed_value = variant_value("interventions_allowed")
if isinstance(backend_value, str) and backend_value not in ("", "rules") and isinstance(allowed_value, str):
    usable = {a.strip() for a in allowed_value.split(",")} - {"", "noop"}
    if not usable:
        print(f"WARN  decision.backend = {backend_value} but interventions_allowed = "
              f"{shown(flags['interventions_allowed'])} allows no intervention: the agent makes no remote "
              "decide call, so nothing triggers (set interventions_allowed to ambient)")

# The persisted layer (#1682): agent-backend.sh writes the backend and model
# persisted, a stage overlay (talk-stage.sh) sits on top of it in memory only.
# A persisted remote backend comes back after a flag-service restart or a
# reboot, whatever the effective value is now.
persisted_backend = (views.get("decision.backend", {}).get("admin_value") or {}).get("variant")
if persisted_backend in ("jev", "claude", "openai"):
    persisted_model = (views.get(f"decision.{persisted_backend}.model", {}).get("admin_value") or {}).get("variant")
    model_note = f" with decision.{persisted_backend}.model = {persisted_model}" if persisted_model else ""
    print(f"WARN  decision.backend {persisted_backend} is persisted{model_note}: a flag-service restart or a "
          f"reboot brings {persisted_backend} back (remote decisions while enabled is on); clear it: "
          "./scripts/talk-stage.sh reset (or ./scripts/agent-backend.sh rules)")
elif persisted_backend:
    print(f"INFO  decision.backend {persisted_backend} is persisted (kept across a restart)")

jev_model = flags.get("decision.jev.model")
if jev_model is not None:
    print(f"INFO  decision.jev.model = {shown(jev_model)} (gate calibrated on jev-1.13.0 = variant pinned)")
jev_url = flags.get("decision.jev.base_url", {})
if "blackhole" not in jev_url.get("variants", {}):
    print("FAIL  decision.jev.base_url has no blackhole variant: the degrade beat (option B) needs the "
          "shipped https://192.0.2.1/v1 variant (restore services/flagd/agent.json from git, restart the flag service)")
elif jev_url.get("defaultVariant") == "blackhole":
    print("FAIL  decision.jev.base_url = blackhole: every jev call times out (reset: default)")
else:
    print(f"PASS  decision.jev.base_url = {shown(jev_url)}; blackhole variant present")

# Variants the beats flip to must exist: agent-backend.sh refuses an undeclared one on stage (#743).
jev_threshold = flags.get("decision.jev.act_confidence_threshold", {}).get("variants", {})
missing_threshold = [v for v in ("strict", "gate") if v not in jev_threshold]
if missing_threshold:
    print(f"FAIL  decision.jev.act_confidence_threshold has no {'/'.join(missing_threshold)} variant: "
          "the threshold beat (3a) cannot run (restore services/flagd/agent.json from git, restart the flag service)")
else:
    print("PASS  decision.jev.act_confidence_threshold has the strict and gate variants")
if "patient" not in flags.get("decision.claude.timeout_ms", {}).get("variants", {}):
    print("WARN  decision.claude.timeout_ms has no patient variant: the Claude timeout recovery (UC-070 A3) "
          "cannot be flipped")

# The optional second vendor (beat 6b) needs a model and a base URL variant the presenter adds.
model = flags.get("decision.openai.model", {})
model_value = model.get("variants", {}).get(model.get("defaultVariant"))
non_empty = [v for v, val in model.get("variants", {}).items() if val]
if not non_empty:
    print("INFO  decision.openai.model has no non-empty variant: the optional openai beat (6b) "
          "falls back to rules (backend_unavailable) until you add the vendor's model + base_url variants")
elif not model_value:
    print(f"PASS  decision.openai.model variants available: {', '.join(non_empty)} "
          "(select with agent-backend.sh openai <variant>)")
else:
    print(f"PASS  decision.openai.model = {shown(model)}")
base_url = flags.get("decision.openai.base_url", {})
print(f"INFO  decision.openai.base_url = {shown(base_url)}; variants: {', '.join(base_url.get('variants', {}))}")

budget = flags.get("llm.max_requests_per_minute")
if budget is not None:
    print(f"INFO  llm.max_requests_per_minute = {shown(budget)} "
          "(global remote-call budget; capped by AGENT_LLM_MAX_REQUESTS_PER_MINUTE when set)")

# Interventions are game-scoped (UC-053 BR-003): what the agent applied lives in
# its game's scope and is dropped when that game ends, so nothing needs a reset.
print(f"PASS  interventions are per game: {service_state.get('scope_values', 0)} value(s) in "
      f"{service_state.get('live_games', 0)} live game(s), dropped when each game ends")
PY
)"
  while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    case "${line}" in
      PASS*) echo "${line}"; n_pass=$((n_pass + 1)) ;;
      WARN*) echo "${line}"; n_warn=$((n_warn + 1)) ;;
      FAIL*) echo "${line}"; n_fail=$((n_fail + 1)) ;;
      *) echo "${line}" ;;
    esac
  done <<<"${flag_out}"
fi

# --- 4. decision backend keys (presence only) ------------------------------
section "decision backend keys (agent container env; values never printed)"
if [[ "${docker_ok}" -eq 1 ]]; then
  agent_env="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' joustmania-agent 2>/dev/null)" || agent_env=""
  if [[ -z "${agent_env}" ]]; then
    fail "joustmania-agent: cannot read its environment"
  else
    key_set() { grep -qE "^$1=[^[:space:]]+" <<<"${agent_env}"; }
    if key_set JEV_API_KEY; then pass "jev: JEV_API_KEY present"; else fail "jev: JEV_API_KEY missing: the main backend falls back to rules (backend_unavailable); put it in .env.local, recreate the agent"; fi
    if key_set ANTHROPIC_API_KEY; then pass "claude: ANTHROPIC_API_KEY present"; else fail "claude: ANTHROPIC_API_KEY missing: no in-game backup and no post-game summary; put it in .env.local, recreate the agent"; fi
    if key_set DECISION_OPENAI_API_KEY; then pass "openai: DECISION_OPENAI_API_KEY present"; else info "openai: DECISION_OPENAI_API_KEY absent (only the optional second-vendor beat needs it)"; fi
    # The post-game text backend is the inference.backend flag only (the
    # AGENT_INFERENCE_BACKEND env was removed, #1907): read the effective value, so a
    # stage overlay counts and an unreachable flag service FAILs.
    inference_flag="$(PYTHONPATH="${SCRIPT_DIR}" python3 -c '
from flagservice import FlagService
try:
    print(FlagService.from_env().effective("agent")["inference.backend"][1])
except Exception:
    print("?")
' 2>/dev/null)"
    inference_flag="$(tr '[:upper:]' '[:lower:]' <<<"${inference_flag}" | tr -d '[:space:]')"
    if [[ "${inference_flag}" == "claude" ]]; then
      pass "inference.backend flag = claude (the post-game summary backend)"
    else
      fail "inference.backend flag = ${inference_flag:-?} (not claude): the closing summary beat shows a capture-only retro; set the flag to claude (agent-backend.sh / flag-service overlay)"
    fi
    # Not a secret: the per-minute budget ceiling, read at agent start. The cap is
    # min(env, flag) (#1788, C-034): the env can only lower the flag, never raise it.
    budget_ceiling="$(grep -E '^AGENT_LLM_MAX_REQUESTS_PER_MINUTE=' <<<"${agent_env}" | head -1 | cut -d= -f2-)"
    flag_budget="$(PYTHONPATH="${SCRIPT_DIR}" python3 -c '
from flagservice import FlagService
try:
    variant, value = FlagService.from_env().effective("agent")["llm.max_requests_per_minute"]
    print(variant, value)
except Exception:
    print("? ?")
' 2>/dev/null)"
    read -r budget_variant budget_value <<<"${flag_budget}"
    if [[ "${budget_ceiling}" =~ ^[0-9]+$ && "${budget_ceiling}" -gt 0 && "${budget_ceiling}" -lt 30 ]]; then
      warn "AGENT_LLM_MAX_REQUESTS_PER_MINUTE=${budget_ceiling} caps the flag below the stage 30/min; only decision.min_interval_ms can relieve it live"
    elif [[ "${budget_value}" =~ ^[0-9]+$ && "${budget_value}" -ge 30 ]]; then
      if [[ "${budget_ceiling}" =~ ^[0-9]+$ && "${budget_ceiling}" -gt 0 ]]; then
        pass "LLM budget: llm.max_requests_per_minute = ${budget_variant} (${budget_value}/min) under the AGENT_LLM_MAX_REQUESTS_PER_MINUTE=${budget_ceiling} ceiling"
      else
        pass "LLM budget: llm.max_requests_per_minute = ${budget_variant} (${budget_value}/min; no env ceiling)"
      fi
    else
      warn "LLM budget: llm.max_requests_per_minute = ${budget_variant:-?} (${budget_value:-?}/min): a remote-decide game wants 15-20/min plus the summary; set the flag to stage (30) (AGENT_LLM_MAX_REQUESTS_PER_MINUTE can only lower it)"
    fi
  fi
fi

# --- 4b. audio cue resolves in the running audio service -------------------
section "audio cue (agent writer default -> running audio container)"
cue="$(sed -nE 's/^[[:space:]]*defaultAudioCue[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "${WRITER_GO}" 2>/dev/null | head -1)"
if [[ -z "${cue}" ]]; then
  cue="agent_cue"
  info "defaultAudioCue not found in ${WRITER_GO}; checking ${cue}"
fi
audio_state=""
if [[ "${docker_ok}" -eq 1 ]]; then
  audio_state="$(docker inspect -f '{{.State.Status}}' joustmania-audio 2>/dev/null)" || audio_state=""
  audio_state="${audio_state%% *}"
fi
if [[ "${docker_ok}" -eq 1 && "${audio_state}" != "running" ]]; then
  # Not an old image: there is nothing to ask. The container section above
  # already names it; say why the cue cannot be checked.
  fail "audio cue '${cue}' not checked: audio container not running (joustmania-audio: ${audio_state:-not found})"
elif [[ "${docker_ok}" -eq 1 ]]; then
  # The running image, not the checkout: a stale image is exactly the trap.
  if cue_out="$(docker exec joustmania-audio python -m services.audio.sound_catalog "${cue}" 2>&1)"; then
    pass "audio cue ${cue_out}"
  else
    fail "audio cue '${cue}' does not resolve in joustmania-audio: ${cue_out:-no output} (play_audio_cue would play nothing; an image older than #1301 has no alias)"
  fi
fi

# --- 5. Jaeger ---------------------------------------------------------------
section "Jaeger (${BASE_URL}/jaeger/)"
if ! have curl; then
  fail "curl not found; HTTP checks skipped"
else
  if curl -fs --max-time 5 -o /dev/null "${BASE_URL}/jaeger/api/services"; then
    pass "Jaeger API reachable"
    end_us="$(($(date +%s) * 1000000))"
    start_us="$((end_us - MINUTES * 60 * 1000000))"
    traces="$(curl -fs --max-time 15 "${BASE_URL}/jaeger/api/traces?service=agent&operation=agent.decide&start=${start_us}&end=${end_us}&limit=5" 2>/dev/null)" || traces=""
    summary="$(python3 -c '
import json, sys
try:
    data = json.load(sys.stdin).get("data") or []
except ValueError:
    print("ERR"); sys.exit(0)
spans = [s for t in data for s in t.get("spans", []) if s.get("operationName") == "agent.decide"]
if not spans:
    print("0"); sys.exit(0)
last = max(spans, key=lambda s: s.get("startTime", 0))
tags = {t["key"]: t.get("value") for t in last.get("tags", [])}
print(len(spans), tags.get("decide.backend"), tags.get("decide.outcome"), tags.get("decide.fallback_reason") or "-")
' <<<"${traces}")"
    read -r count backend outcome reason <<<"${summary}"
    if [[ "${count}" == "ERR" || -z "${count}" ]]; then
      fail "agent.decide query failed (Jaeger slow or not answering)"
    elif [[ "${count}" -eq 0 ]]; then
      fail "no agent.decide spans in the last ${MINUTES} min (play a warm-up game first; the rules path only emits on cycles that decide)"
    else
      pass "${count} agent.decide span(s) in the last ${MINUTES} min; newest: backend=${backend} outcome=${outcome} fallback_reason=${reason}"
    fi
  else
    fail "Jaeger API not reachable at ${BASE_URL}/jaeger/api/services"
  fi

  # --- 6. dashboard ----------------------------------------------------------
  section "dashboard (${BASE_URL}/)"
  if curl -fs --max-time 5 -o /dev/null "${BASE_URL}/"; then
    pass "dashboard reachable"
  else
    fail "dashboard not reachable at ${BASE_URL}/"
  fi
  if curl -fs --max-time 5 -o /dev/null "${BASE_URL}/grafana/api/health"; then
    pass "Grafana reachable"
  else
    warn "Grafana not reachable at ${BASE_URL}/grafana/ (not needed for the core beats)"
  fi

  # --- 7. clock ----------------------------------------------------------------
  section "clock"
  if have timedatectl; then
    ntp="$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" || ntp=""
    info "timedatectl NTPSynchronized=${ntp:-unknown}"
  fi
  ref_epoch=""
  ref_url=""
  for url in ${CLOCK_URLS}; do
    date_hdr="$(curl -sSI --max-time 5 "${url}" 2>/dev/null | tr -d '\r' | awk 'tolower($1) == "date:" { sub(/^[^:]*:[ ]*/, ""); print; exit }')"
    # python parses the RFC 7231 date the same way on Linux and macOS (BSD date has no -d).
    if [[ -n "${date_hdr}" ]] && ref_epoch="$(python3 -c 'import sys, email.utils; print(int(email.utils.parsedate_to_datetime(sys.argv[1]).timestamp()))' "${date_hdr}" 2>/dev/null)"; then
      ref_url="${url}"
      break
    fi
    ref_epoch=""
  done
  if [[ -z "${ref_epoch}" ]]; then
    warn "no reference host answered (${CLOCK_URLS}); offline? check the clock by hand: date -u"
  else
    skew=$(($(date +%s) - ref_epoch))
    abs_skew="${skew#-}"
    if [[ "${abs_skew}" -gt "${MAX_SKEW}" ]]; then
      warn "clock skew ${skew}s vs ${ref_url} (> ${MAX_SKEW}s): fix NTP before the talk (Jaeger lookback searches and Grafana assume a sane clock)"
    else
      pass "clock skew ${skew}s vs ${ref_url}"
    fi
    # Outbound HTTPS from this host works; it does not prove a backend key or model works.
    info "outbound HTTPS works from this host (${ref_url} answered)"
  fi

  # --- 8. real PS Move controllers ---------------------------------------------
  section "real controllers (controller-manager via ${BASE_URL}/)"
  # Read the fallback state from the flag service (the same rule as section 3).
  fallback_on="$(PYTHONPATH="${SCRIPT_DIR}" python3 -c '
import json, sys
from flagservice import FlagService
try:
    want = json.load(open(sys.argv[1])).get("fallback", {}).get("llm.eligible_game_kinds")
    variant = FlagService.from_env().effective("agent")["llm.eligible_game_kinds"][0]
    print("1" if want and variant == want else "0")
except Exception:
    print("0")
' "${BASELINE_FILE}" 2>/dev/null)"
  # short_of <message>: FAIL on stage, WARN while the fallback carries the talk.
  short_of() {
    if [[ "${fallback_on}" == "1" ]]; then
      warn "$* (fallback on: the talk runs on shadow games, UC-070 A9)"
    else
      fail "$* (pair them, see docs/hardware-setup-guide.md; or ./scripts/talk-stage.sh fallback on)"
    fi
  }
  if ! [[ "${MIN_CONTROLLERS}" =~ ^[0-9]+$ ]]; then
    fail "TALK_MIN_CONTROLLERS=${MIN_CONTROLLERS} is not a number"
  else
    roster="$(curl -fs --max-time 5 -X POST -H 'Content-Type: application/json' -d '{}' \
      "${BASE_URL}/joustmania.controller_manager.ControllerManagerService/GetConnectedControllers" 2>/dev/null)" || roster=""
    # Mock serials are MOCK0001 / mock_controller_N (mock_adapter.py); reserved
    # (pattern) controllers are never listed. Anything else is a real controller.
    counted="$(python3 -c '
import json, re, sys
try:
    body = json.load(sys.stdin)
except ValueError:
    print("ERR"); sys.exit(0)
if not isinstance(body, dict):
    print("ERR"); sys.exit(0)
serials = body.get("connectedSerials") or [c.get("serial", "") for c in body.get("controllers") or []]
real = [s for s in serials if s and not re.match(r"(?i)mock", s)]
print(len(real), len(serials) - len(real), " ".join(real) or "-")
' <<<"${roster}")"
    read -r n_real n_mock real_serials <<<"${counted}"
    if [[ -z "${roster}" || "${n_real}" == "ERR" || -z "${n_real}" ]]; then
      # Not a pairing problem, and the fallback cannot help: its shadow games run
      # on controller-manager's pattern controllers too.
      msg="controller-manager not reachable at ${BASE_URL} (GetConnectedControllers): cannot count real controllers"
      if [[ "${fallback_on}" == "1" ]]; then
        warn "${msg}; the fallback's shadow games need controller-manager too (check the containers section)"
      else
        fail "${msg} (check the containers section; the fallback needs controller-manager as well)"
      fi
    elif [[ "${n_real}" -ge "${MIN_CONTROLLERS}" ]]; then
      pass "${n_real} real controller(s) connected (need ${MIN_CONTROLLERS}): ${real_serials}"
    else
      short_of "${n_real} real controller(s) connected, need ${MIN_CONTROLLERS} (${n_mock} mock not counted)"
    fi
  fi
fi

echo
echo "${n_pass} PASS, ${n_warn} WARN, ${n_fail} FAIL"
[[ "${n_fail}" -eq 0 ]]
