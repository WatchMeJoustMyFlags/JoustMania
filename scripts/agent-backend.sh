#!/usr/bin/env bash
# Flip the agent DECISION BACKEND on stage (#1232, epic #1225; epic #1407).
#
# The decision backend, its model, the act-confidence threshold and GenAI
# content capture are flags in the agent flag set (decision.* keys, #1226). This
# helper changes them through the flag service (docs/specs/services.md §7.2) as
# stack overlays. The backend and the per-backend model are written persisted
# (C-018, #1682), so a flag-service restart keeps the choice; the other settings
# stay in memory. The flag files never change (C-033).
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
# An unknown variant is refused by the flag service (422 out_of_bounds) with
# the list of valid ones. The change is pushed to flagd at once; the agent reads
# the decision.* flags at use time, so no agent restart is needed. API keys stay
# env-only (ANTHROPIC_API_KEY, DECISION_OPENAI_API_KEY, JEV_API_KEY); a backend
# without its key/model falls back to rules.
#
# Env: FLAG_SERVICE_URL (default http://localhost/flags, the envoy route).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRITER="${FLAGS_WRITER:-stage}"

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

PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 - "${WRITER}" "$@" <<'PY'
import json
import sys

from flagservice import FlagService, FlagServiceError, FlagServiceUnreachable

writer, args = sys.argv[1], sys.argv[2:]
action = args[0] if args else "status"
fs = FlagService.from_env()
SET = "agent"


def show(value):
    return json.dumps(value) if isinstance(value, str) else json.dumps(value).lower()


def flag_view(key):
    try:
        return fs.flag(SET, key)
    except FlagServiceError as err:
        if err.status == 404 and err.reason == "unknown_flag":
            sys.exit(f"flag {key!r} not defined in the {SET} flag set")
        if err.reason == "non_json":
            sys.exit(f"HTTP {err.status} from {fs.base_url}: {err.detail} (is FLAG_SERVICE_URL the flag service?)")
        raise


def set_variant(key, variant, persist=False):
    flag_view(key)  # a missing flag is named, not a raw 404
    try:
        fs.set_variant(SET, key, variant, writer=writer, persist=persist)
    except FlagServiceError as err:
        if err.reason == "out_of_bounds":
            view = fs.flag(SET, key)
            sys.exit(f"variant {variant!r} not defined for flag {key!r}; valid: {', '.join(view['variants'])}")
        sys.exit(f"flag service refused {key}={variant}: {err.reason}: {err.detail}")
    view = fs.flag(SET, key)
    return show(view["variant_values"].get(variant))


try:
    if action == "status":
        flags = fs.effective(SET)

        def current(key):
            if key not in flags:
                return "MISSING (flag not served)"
            variant, value = flags[key]
            return f"{variant} ({show(value)})"

        backend = flags.get("decision.backend", (None, None))[1]
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
        print(f"({fs.base_url})")
        sys.exit(0)

    if action == "threshold":
        value = set_variant("decision.act_confidence_threshold", args[1])
        print(f"agent act threshold -> {args[1]} ({value})")
    elif action == "jev-threshold":
        # Jev's own bar (#1287): its confidence is the calibrated gate p_yes, so it
        # has its own threshold; "inherit" makes jev use the global one.
        value = set_variant("decision.jev.act_confidence_threshold", args[1])
        print(f"agent jev act threshold -> {args[1]} ({value})")
    elif action == "effort":
        value = set_variant("decision.openai.reasoning_effort", args[1])
        print(f"agent openai reasoning effort -> {args[1]} ({value})")
    elif action == "capture":
        set_variant("decision.capture_content", args[1])
        print(f"agent content capture -> {args[1]}")
    else:
        backend, model = action, (args[1] if len(args) > 1 else None)
        if model is not None and backend == "rules":
            sys.exit("the rules backend has no model; usage: agent-backend.sh rules")
        if model is not None:
            # Validate the model first, so a bad model never leaves the backend switched.
            view = flag_view(f"decision.{backend}.model")
            if model not in view["variants"]:
                sys.exit(f"variant {model!r} not defined for flag 'decision.{backend}.model'; valid: {', '.join(view['variants'])}")
        # Validate the backend variant too, then write the model first and the backend
        # last: a failed write leaves the previous backend active (UC-044 A4, Post-F-1).
        backend_view = flag_view("decision.backend")
        if backend not in backend_view["variants"]:
            sys.exit(f"variant {backend!r} not defined for flag 'decision.backend'; valid: {', '.join(backend_view['variants'])}")
        msg = f"agent backend -> {backend}"
        if model is not None:
            value = set_variant(f"decision.{backend}.model", model, persist=True)
        set_variant("decision.backend", backend, persist=True)
        if model is not None:
            msg += f"  model -> {model} ({value})"
        print(msg)
except FlagServiceUnreachable as err:
    sys.exit(str(err))
except FlagServiceError as err:
    sys.exit(f"flag service: {err.reason}: {err.detail}")
PY
