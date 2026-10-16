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
#   ./scripts/agent-backend.sh threshold strict      # decision.act_confidence_threshold -> strict
#   ./scripts/agent-backend.sh capture on            # decision.capture_content -> on
#
# Only defaultVariant is edited; an unknown variant is refused with the list of
# valid ones read from the file. The write is atomic (temp file in the same dir
# + rename), so flagd never reads a half-written file.
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
       $0 threshold {low|default|strict}
       $0 capture {on|off}
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
  threshold|capture)
    [[ $# -eq 2 ]] || usage
    ;;
  *)
    usage
    ;;
esac

python3 - "${FLAG_FILE}" "$@" <<'PY'
import json
import os
import sys
import tempfile

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


def write_atomic():
    # Temp file in the SAME dir + os.replace: flagd sees either the old or the new
    # file, never a truncated one. Keep the original mode (mkstemp creates 0600,
    # which the nonroot flagd container could not read).
    directory = os.path.dirname(os.path.abspath(path))
    mode = os.stat(path).st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".agent-backend-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


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
    print(f"capture   = {current('decision.capture_content')}")
    print(f"({path})")
    missing = [
        k
        for k in ("decision.backend", "decision.act_confidence_threshold", "decision.capture_content")
        if k not in flags
    ]
    sys.exit(1 if missing else 0)

if action == "threshold":
    value = set_variant("decision.act_confidence_threshold", args[1])
    write_atomic()
    print(f"agent act threshold -> {args[1]} ({value})  ({path})")
elif action == "capture":
    value = set_variant("decision.capture_content", args[1])
    write_atomic()
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
    write_atomic()
    print(f"{msg}  ({path})")
PY
