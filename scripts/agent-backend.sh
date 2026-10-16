#!/usr/bin/env bash
# Flip the agent DECISION BACKEND on stage (#1232, epic #1225).
#
# The decision backend, its model, the act-confidence threshold and GenAI
# content capture are all flagd flags in the agent domain (decision.* keys, see
# #1226). This helper flips their defaultVariant in the CI/DRY-RUN flag dir only
# (services/flagd/ci/agent.json, the dir the demo stack serves; same file
# ./scripts/agent-killswitch.sh mutates). It NEVER touches the production
# services/flagd/agent.json defaults.
#
# Usage:
#   ./scripts/agent-backend.sh                       # show current settings
#   ./scripts/agent-backend.sh status                # same
#   ./scripts/agent-backend.sh claude                # decision.backend -> claude
#   ./scripts/agent-backend.sh claude sonnet         # ... and decision.claude.model -> sonnet
#   ./scripts/agent-backend.sh rules                 # back to the deterministic rules backend
#   ./scripts/agent-backend.sh threshold strict      # decision.act_confidence_threshold -> strict (global: claude/openai)
#   ./scripts/agent-backend.sh jev-threshold inherit # decision.jev.act_confidence_threshold -> inherit (use the global)
#   ./scripts/agent-backend.sh capture on            # decision.capture_content -> on
#   ./scripts/agent-backend.sh effort low            # decision.openai.reasoning_effort -> low
#
# Only defaultVariant is edited; an unknown variant is refused with the list of
# valid ones read from the file. The write is in place (same inode), like
# agent-killswitch.sh; see write_in_place() for why.
#
# After flipping, flagd hot-reloads the file and the change takes effect LIVE
# (~1 s): the agent reads the decision.* flags at use time, every cycle, so no
# agent restart is needed. API keys stay env-only (ANTHROPIC_API_KEY,
# DECISION_OPENAI_API_KEY, JEV_API_KEY); a backend without its key/model falls
# back to rules.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FLAG_FILE="${AGENT_FLAG_FILE:-${REPO_ROOT}/services/flagd/ci/agent.json}"

usage() {
  cat >&2 <<EOF
usage: $0 [status]
       $0 {rules|claude|openai|jev} [model-variant]
       $0 threshold {low|gate|default|strict}
       $0 jev-threshold {inherit|gate|default|strict}
       $0 capture {on|off}
       $0 effort {auto|none|minimal|low|omit}
EOF
  exit 2
}

action="${1:-status}"

case "${action}" in
  status)
    [[ $# -le 1 ]] || usage
    ;;
  rules|claude|openai|jev)
    [[ $# -le 2 ]] || usage
    ;;
  threshold|jev-threshold|capture|effort)
    [[ $# -eq 2 ]] || usage
    ;;
  *)
    usage
    ;;
esac

python3 - "${FLAG_FILE}" "$@" <<'PY'
import json
import sys

path, args = sys.argv[1], sys.argv[2:]
action = args[0] if args else "status"

try:
    with open(path) as fh:
        data = json.load(fh)
except FileNotFoundError:
    sys.exit(f"flag file not found: {path}")
flags = data.get("flags", {})


def require(key):
    if key not in flags:
        sys.exit(f"flag {key!r} not defined in {path} (decision.* flags land with #1226)")
    return flags[key]


def show(value):
    return json.dumps(value) if isinstance(value, str) else json.dumps(value).lower()


def current(key):
    """'variant (value)' for display, or a MISSING marker."""
    if key not in flags:
        return "MISSING (flag not in file)"
    flag = flags[key]
    variant = flag.get("defaultVariant")
    return f"{variant} ({show(flag.get('variants', {}).get(variant))})"


def set_variant(key, variant):
    flag = require(key)
    variants = flag.get("variants", {})
    if variant not in variants:
        valid = ", ".join(variants)
        sys.exit(f"variant {variant!r} not defined for flag {key!r} in {path}; valid: {valid}")
    flag["defaultVariant"] = variant
    return show(variants[variant])


def write_in_place():
    # In place on purpose: works with directory AND single-file bind mounts (a rename leaves a file mount pinned to the old inode); flagd stat-polls the mtime, so a torn read recovers on the next poll.
    payload = json.dumps(data, indent=2) + "\n"
    with open(path, "w") as fh:
        fh.write(payload)


if action == "status":
    backend_flag = flags.get("decision.backend", {})
    backend = backend_flag.get("variants", {}).get(backend_flag.get("defaultVariant"))
    print(f"backend   = {current('decision.backend')}")
    if backend is None:
        print("model     = n/a")
    elif backend == "rules":
        print("model     = n/a (rules backend)")
    else:
        print(f"model     = {current(f'decision.{backend}.model')}")
    print(f"threshold = {current('decision.act_confidence_threshold')}")
    print(f"jev thr.  = {current('decision.jev.act_confidence_threshold')}")
    print(f"capture   = {current('decision.capture_content')}")
    if backend == "openai":
        print(f"effort    = {current('decision.openai.reasoning_effort')}")
    print(f"({path})")
    missing = [
        k
        for k in ("decision.backend", "decision.act_confidence_threshold", "decision.capture_content")
        if k not in flags
    ]
    sys.exit(1 if missing else 0)

if action == "threshold":
    value = set_variant("decision.act_confidence_threshold", args[1])
    write_in_place()
    print(f"agent act threshold -> {args[1]} ({value})  ({path})")
elif action == "jev-threshold":
    # Jev's own bar (#1287): its confidence is the calibrated gate p_yes, so it
    # has its own threshold; "inherit" makes jev use the global one.
    value = set_variant("decision.jev.act_confidence_threshold", args[1])
    write_in_place()
    print(f"agent jev act threshold -> {args[1]} ({value})  ({path})")
elif action == "effort":
    value = set_variant("decision.openai.reasoning_effort", args[1])
    write_in_place()
    print(f"agent openai reasoning effort -> {args[1]} ({value})  ({path})")
elif action == "capture":
    value = set_variant("decision.capture_content", args[1])
    write_in_place()
    print(f"agent content capture -> {args[1]}  ({path})")
else:
    backend, model = action, (args[1] if len(args) > 1 else None)
    if model is not None and backend == "rules":
        sys.exit("the rules backend has no model; usage: agent-backend.sh rules")
    set_variant("decision.backend", backend)
    msg = f"agent backend -> {backend}"
    if model is not None:
        value = set_variant(f"decision.{backend}.model", model)
        msg += f"  model -> {model} ({value})"
    write_in_place()
    print(f"{msg}  ({path})")
PY
