#!/usr/bin/env bash
# Enable the experiment cohort loop for a `make dry-run` (#1077).
#
# After #1044 migrated the experiment config from env vars to flagd flags, the
# LIVE gate for the cohort loop is the `experiments_enabled` flag in the agent
# flagd domain — and it OVERRIDES the `AGENT_EXPERIMENTS_ENABLED=true` env the
# dry-run override sets. Both that flag and the master `enabled` kill-switch are
# fail-closed `off` by DEFAULT in BOTH the production flag dir
# (services/flagd/agent.json) and the ci/dry-run dir (services/flagd/ci/agent.json).
# So a plain `make dry-run` produces ZERO experiment activity until they are
# flipped on.
#
# This helper flips, in the CI/DRY-RUN flag dir ONLY (the dir `make dry-run`
# serves — same file ./scripts/agent-killswitch.sh mutates):
#   - `enabled`             -> on   (master kill-switch; mirrors agent-killswitch.sh)
#   - `experiments_enabled` -> on   (the LIVE experiment-loop gate, post-#1044)
#   - `mode`                -> llm  (ONLY when AGENT_INFERENCE_BACKEND=openai is
#                                    set; otherwise `mode` is left untouched so
#                                    the stub backend stays on `rules`)
#
# It NEVER touches the production services/flagd/agent.json defaults — those stay
# fail-closed by design.
#
# Usage:
#   ./scripts/agent-dryrun-enable.sh on     # enable experiments (dry-run)
#   ./scripts/agent-dryrun-enable.sh off    # restore fail-closed defaults
#   ./scripts/agent-dryrun-enable.sh status
#
# `mode=llm` is applied on `on` only when an inference backend is configured:
#   AGENT_INFERENCE_BACKEND=openai ./scripts/agent-dryrun-enable.sh on
#
# After flipping, flagd hot-reloads the file; restart the agent so the loop
# re-reads the existence layer:
#   docker compose -f docker-compose.yml -f docker-compose.ci.yml --profile agent restart agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FLAG_FILE="${AGENT_FLAG_FILE:-${REPO_ROOT}/services/flagd/ci/agent.json}"
INFERENCE_BACKEND="${AGENT_INFERENCE_BACKEND:-}"

action="${1:-status}"

case "${action}" in
  on|off)
    python3 - "${FLAG_FILE}" "${action}" "${INFERENCE_BACKEND}" <<'PY'
import json
import sys

path, action, backend = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    data = json.load(fh)

flags = data["flags"]


def set_variant(name, variant):
    flag = flags[name]
    if variant not in flag["variants"]:
        sys.exit(f"variant {variant!r} not defined for flag {name!r} in {path}")
    flag["defaultVariant"] = variant
    print(f"  {name} -> {variant}")


if action == "on":
    set_variant("enabled", "on")
    set_variant("experiments_enabled", "on")
    if backend == "openai":
        # Exercise the inference backend only when one is configured.
        set_variant("mode", "llm")
    else:
        print("  mode -> (unchanged; set AGENT_INFERENCE_BACKEND=openai for llm)")
else:  # off -> restore the fail-closed defaults
    set_variant("enabled", "off")
    set_variant("experiments_enabled", "off")
    set_variant("mode", "rules")

with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print(f"dry-run experiment gate -> {action}  ({path})")
PY
    ;;
  status)
    python3 - "${FLAG_FILE}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path) as fh:
    data = json.load(fh)
flags = data["flags"]
for name in ("enabled", "experiments_enabled", "mode"):
    print(f"{name} = {flags[name]['defaultVariant']}")
print(f"({path})")
PY
    ;;
  *)
    echo "usage: $0 {on|off|status}" >&2
    exit 2
    ;;
esac
