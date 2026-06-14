#!/usr/bin/env bash
# Flip the agent MASTER kill-switch (#999 dry-run helper).
#
# The agent's `enabled` flag (agent flagd domain) is the master kill-switch and
# is fail-closed `off` by DEFAULT in BOTH the production flag dir
# (services/flagd/agent.json) and the ci/dry-run dir (services/flagd/ci/agent.json).
# With it off the experiment cohort loop declares + writes targeting then aborts:
#   experiment.torn_down ... reason="kill-switch: agent disabled"
#
# This helper toggles the `enabled` defaultVariant in the CI/DRY-RUN flag dir
# only (the dir `make dry-run` serves). It NEVER touches the production
# services/flagd/agent.json default — that stays fail-closed by design.
#
# Usage:
#   ./scripts/agent-killswitch.sh on    # enable the agent (dry-run)
#   ./scripts/agent-killswitch.sh off   # disable it again
#   ./scripts/agent-killswitch.sh status
#
# After flipping, flagd hot-reloads the file; restart the agent so the loop
# re-reads the existence layer:
#   docker compose -f docker-compose.yml -f docker-compose.ci.yml --profile agent restart agent
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

path, variant = sys.argv[1], sys.argv[2]
with open(path) as fh:
    data = json.load(fh)

flag = data["flags"]["enabled"]
if variant not in flag["variants"]:
    sys.exit(f"variant {variant!r} not defined in {path}")
flag["defaultVariant"] = variant

with open(path, "w") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
print(f"agent kill-switch -> {variant}  ({path})")
PY
    ;;
  status)
    python3 - "${FLAG_FILE}" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path) as fh:
    data = json.load(fh)
print(f"agent kill-switch = {data['flags']['enabled']['defaultVariant']}  ({path})")
PY
    ;;
  *)
    echo "usage: $0 {on|off|status}" >&2
    exit 2
    ;;
esac
