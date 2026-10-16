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
#
# The in-game decision backend is NOT touched here: flip it with
# ./scripts/agent-backend.sh (decision.backend, #1232).
#
# It NEVER touches the production services/flagd/agent.json defaults — those stay
# fail-closed by design.
#
# Usage:
#   ./scripts/agent-dryrun-enable.sh on     # enable experiments (dry-run)
#   ./scripts/agent-dryrun-enable.sh off    # restore fail-closed defaults
#   ./scripts/agent-dryrun-enable.sh status
#
# After flipping, flagd hot-reloads the file and the gate flips take effect LIVE
# (~1 s): since #1044 the experiment loop is always built and self-gates each tick
# on the live experiments_enabled flag, so an off→on flip starts it with NO agent
# restart. A restart/recreate is only required when you set/change the
# AGENT_INFERENCE_* env vars (the proposer/retro text backend, read at process
# start), e.g. AGENT_INFERENCE_BACKEND=claude with ANTHROPIC_API_KEY exported:
#   docker compose -f docker-compose.yml -f docker-compose.ci.yml --profile agent up -d agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FLAG_FILE="${AGENT_FLAG_FILE:-${REPO_ROOT}/services/flagd/ci/agent.json}"

action="${1:-status}"

case "${action}" in
  on|off)
    python3 - "${FLAG_FILE}" "${action}" <<'PY'
import json
import sys

path, action = sys.argv[1], sys.argv[2]
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
else:  # off -> restore the fail-closed defaults
    set_variant("enabled", "off")
    set_variant("experiments_enabled", "off")

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
for name in ("enabled", "experiments_enabled", "decision.backend"):
    print(f"{name} = {flags[name]['defaultVariant']}")
print(f"({path})")
PY
    ;;
  *)
    echo "usage: $0 {on|off|status}" >&2
    exit 2
    ;;
esac
