#!/usr/bin/env bash
# Read-only pre-flight for the "Two Standards Walk Into an Agent" talk (#743,
# epic #1225). Runbook: docs/talk/two-standards-runbook.md.
#
# Prints one PASS / WARN / FAIL / INFO line per check and exits non-zero when
# any check FAILs. It changes NOTHING: it only reads the flag file, inspects
# containers, and sends GET/HEAD requests.
#
# Checks:
#   1. the stack containers are running (and healthy where they have a check)
#   2. the flag file the helper scripts edit is the one flagd and the agent mount
#   3. the stage baseline of the decision.* + safety flags (defaultVariant),
#      and the interventions.json next to it back at its neutral variants
#   4. the agent container env (presence only; key values are never printed):
#      JEV_API_KEY (Jev, the main in-game backend) and ANTHROPIC_API_KEY (Claude:
#      in-game backup + post-game summary) present, AGENT_INFERENCE_BACKEND=claude
#      (the closing summary beat), and the shared LLM budget >= 30/min (the
#      AGENT_LLM_MAX_REQUESTS_PER_MINUTE override, else the flag's value)
#   5. Jaeger reachable and holding agent.decide spans from the last N minutes
#   6. the dashboard reachable through envoy
#   7. the clock: local time vs the HTTP Date header of a reachable host
#
# Usage:
#   ./scripts/talk-preflight.sh                  # all checks
#   ./scripts/talk-preflight.sh --minutes 30     # wider Jaeger window
#
# Environment:
#   AGENT_FLAG_FILE      flag file the helpers edit (default services/flagd/ci/agent.json;
#                        on the Pi hardware stack: services/flagd/agent.json)
#   TALK_BASE_URL        envoy entry point (default http://localhost)
#   TALK_EXPECT_BACKEND  expected decision.backend variant (default rules: the
#                        session starts on the floor, beat 2 flips to jev)
#   TALK_CLOCK_URLS      space-separated URLs whose Date header is the time
#                        reference (default: api.anthropic.com, cloudflare, google)
#   TALK_MAX_SKEW        allowed clock skew in seconds (default 60)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FLAG_FILE="${AGENT_FLAG_FILE:-${REPO_ROOT}/services/flagd/ci/agent.json}"
BASE_URL="${TALK_BASE_URL:-http://localhost}"
BASE_URL="${BASE_URL%/}"
EXPECT_BACKEND="${TALK_EXPECT_BACKEND:-rules}"
CLOCK_URLS="${TALK_CLOCK_URLS:-https://api.anthropic.com https://www.cloudflare.com https://www.google.com}"
MAX_SKEW="${TALK_MAX_SKEW:-60}"
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
  joustmania-flagd joustmania-agent joustmania-otel-collector joustmania-jaeger
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

# --- 2. flag dir: the file the helpers edit is the file flagd serves -------
section "flag dir (helpers edit = flagd serves)"
flag_dir="$(cd "$(dirname "${FLAG_FILE}")" 2>/dev/null && pwd -P)" || flag_dir=""
if [[ ! -f "${FLAG_FILE}" ]]; then
  fail "flag file not found: ${FLAG_FILE}"
else
  info "helpers edit ${FLAG_FILE}"
fi
if [[ "${docker_ok}" -eq 1 && -n "${flag_dir}" ]]; then
  for name in joustmania-flagd joustmania-agent; do
    if ! docker inspect -f '{{.Id}}' "${name}" >/dev/null 2>&1; then
      fail "${name}: container not found, cannot check its flag mount"
      continue
    fi
    src="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/etc/flagd"}}{{.Source}}{{end}}{{end}}' "${name}" 2>/dev/null)" || src=""
    if [[ -z "${src}" ]]; then
      fail "${name}: no /etc/flagd mount found"
    elif [[ "${src}" == "${flag_dir}" ]]; then
      pass "${name}: mounts ${src}"
    else
      fail "${name}: mounts ${src}, but the helpers edit ${flag_dir} (set AGENT_FLAG_FILE to the served dir)"
    fi
  done
fi

# --- 3. stage baseline flags -----------------------------------------------
section "stage baseline flags ($(basename "${FLAG_FILE}"))"
if [[ -f "${FLAG_FILE}" ]]; then
  # The python prints the result lines itself (PASS/WARN/FAIL prefix); count them.
  flag_out="$(python3 - "${FLAG_FILE}" "${EXPECT_BACKEND}" <<'PY'
import json
import os
import sys

path, expect_backend = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        flags = json.load(fh).get("flags", {})
except (OSError, ValueError) as exc:
    print(f"FAIL  cannot parse {path}: {exc}")
    sys.exit(0)

# flag key -> expected defaultVariant for the start of the talk.
BASELINE = {
    "enabled": "on",
    "interventions_enabled": "on",
    "interventions_allowed": "ambient",
    "rollout_enabled": "off",
    "experiments_enabled": "off",
    "decision.backend": expect_backend,
    "decision.act_confidence_threshold": "default",
    # Jev's own bar (#1287): the calibrated gate p_yes acts at 0.6.
    "decision.jev.act_confidence_threshold": "gate",
    "decision.capture_content": "on",
    "decision.min_interval_ms": "default",
    "decision.claude.model": "haiku",
    # Claude backup: 5000 ms covers Haiku's ~3 s warm latency (patient = 8000).
    "decision.claude.timeout_ms": "default",
    "decision.jev.timeout_ms": "default",
    "llm.eligible_game_kinds": "real_only",
}


def shown(flag):
    variant = flag.get("defaultVariant")
    return f"{variant} ({json.dumps(flag.get('variants', {}).get(variant))})"


for key, want in BASELINE.items():
    flag = flags.get(key)
    if flag is None:
        print(f"FAIL  {key}: not defined in the flag file")
    elif flag.get("state") != "ENABLED":
        print(f"FAIL  {key}: state {flag.get('state')}")
    elif flag.get("defaultVariant") == want:
        print(f"PASS  {key} = {shown(flag)}")
    else:
        print(f"FAIL  {key} = {shown(flag)}, stage baseline is {want}")

jev_model = flags.get("decision.jev.model")
if jev_model is not None:
    print(f"INFO  decision.jev.model = {shown(jev_model)} (gate calibrated on jev-1.13.0 = variant pinned)")
jev_url = flags.get("decision.jev.base_url", {})
if "blackhole" not in jev_url.get("variants", {}):
    print("FAIL  decision.jev.base_url has no blackhole variant: the degrade beat (option B) needs the "
          "shipped https://192.0.2.1/v1 variant (restore the flag file from git)")
elif jev_url.get("defaultVariant") == "blackhole":
    print("FAIL  decision.jev.base_url = blackhole: every jev call times out (reset: default)")
else:
    print(f"PASS  decision.jev.base_url = {shown(jev_url)}; blackhole variant present")

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
          "(global remote-call budget; replaced by AGENT_LLM_MAX_REQUESTS_PER_MINUTE when set)")

# interventions.json (same dir): what the agent applied during a run stays there.
# Neutral = the shipped default: "none" if the flag has it, else "default", else "off".
ivpath = os.path.join(os.path.dirname(path), "interventions.json")
try:
    with open(ivpath) as fh:
        ivflags = json.load(fh).get("flags", {})
except FileNotFoundError:
    print(f"WARN  {ivpath} not found")
    ivflags = None
except ValueError as exc:
    print(f"FAIL  cannot parse {ivpath}: {exc} (flagd rejects the whole file)")
    ivflags = None
if ivflags is not None:
    dirty = []
    for key, flag in ivflags.items():
        variants = flag.get("variants", {})
        neutral = next((v for v in ("none", "default", "off") if v in variants), None)
        if neutral is not None and flag.get("defaultVariant") != neutral:
            dirty.append(f"{key}={flag.get('defaultVariant')}")
        if flag.get("targeting"):
            dirty.append(f"{key}(targeting)")
    if dirty:
        print(f"WARN  interventions.json not neutral: {', '.join(dirty)} "
              f"(reset: git checkout {ivpath})")
    else:
        print("PASS  interventions.json at neutral variants")
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
    # Not a secret: the post-game text backend, read at agent start.
    inference_backend="$(grep -E '^AGENT_INFERENCE_BACKEND=' <<<"${agent_env}" | head -1 | cut -d= -f2- | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    if [[ "${inference_backend}" == "claude" ]]; then
      pass "AGENT_INFERENCE_BACKEND=claude (post-game summary on Claude)"
    else
      warn "AGENT_INFERENCE_BACKEND=${inference_backend:-unset} (not claude): the closing summary beat shows a capture-only retro; set it in .env.local, recreate the agent"
    fi
    # Not a secret: the per-minute budget override, read at agent start.
    budget_override="$(grep -E '^AGENT_LLM_MAX_REQUESTS_PER_MINUTE=' <<<"${agent_env}" | head -1 | cut -d= -f2-)"
    if [[ "${budget_override}" =~ ^[0-9]+$ && "${budget_override}" -ge 30 ]]; then
      pass "AGENT_LLM_MAX_REQUESTS_PER_MINUTE=${budget_override} (one remote-decide game + the post-game summary; replaces the flag)"
    elif [[ "${budget_override}" =~ ^[0-9]+$ && "${budget_override}" -gt 0 ]]; then
      warn "AGENT_LLM_MAX_REQUESTS_PER_MINUTE=${budget_override} replaces the flag but is below the stage 30/min; only decision.min_interval_ms can relieve it live"
    else
      flag_budget="$(python3 -c '
import json, sys
try:
    flag = json.load(open(sys.argv[1]))["flags"]["llm.max_requests_per_minute"]
    print(flag["defaultVariant"], flag["variants"][flag["defaultVariant"]])
except (OSError, ValueError, KeyError, TypeError):
    print("? ?")
' "${FLAG_FILE}" 2>/dev/null)"
      read -r budget_variant budget_value <<<"${flag_budget}"
      if [[ "${budget_value}" =~ ^[0-9]+$ && "${budget_value}" -ge 30 ]]; then
        pass "LLM budget: llm.max_requests_per_minute = ${budget_variant} (${budget_value}/min; no env override)"
      else
        warn "LLM budget: llm.max_requests_per_minute = ${budget_variant:-?} (${budget_value:-?}/min) and no env override: a remote-decide game wants 15-20/min plus the summary; set the flag to stage (30)"
      fi
    fi
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
fi

echo
echo "${n_pass} PASS, ${n_warn} WARN, ${n_fail} FAIL"
[[ "${n_fail}" -eq 0 ]]
