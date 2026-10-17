#!/usr/bin/env bash
# Flip the agent MASTER kill-switch (#999 dry-run helper; epic #1407).
#
# The agent's `enabled` flag (agent flag set) is the master kill-switch and is
# fail-closed `off` in the baseline (services/flagd/agent.json and ci/agent.json).
# This helper changes it through the flag service (scripts/flags.sh): `on` sets an
# in-memory stack overlay, `off` clears it, so the fail-closed baseline returns.
# Nothing is persisted (re-review N4): a flag-service restart is `off` again.
#
# Usage:
#   ./scripts/agent-killswitch.sh on    # enable the agent
#   ./scripts/agent-killswitch.sh off   # back to the fail-closed baseline
#   ./scripts/agent-killswitch.sh status
#
# The change takes effect LIVE (the flag service pushes it to flagd at once): the
# agent reads `enabled` each cycle, so no agent restart is needed.
#
# If the flag service cannot be reached, `off` cannot clear the overlay: the
# helper then prints the fallback brake (stop the agent container; the game keeps
# running without it) and exits non-zero.
#
# Env: FLAG_SERVICE_URL (default http://localhost/flags, the envoy route),
#      AGENT_CONTAINER (default joustmania-agent, named in the fallback).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAGS="${FLAGS_SH:-${SCRIPT_DIR}/flags.sh}"
export FLAGS_WRITER="${FLAGS_WRITER:-operator}"
AGENT_CONTAINER="${AGENT_CONTAINER:-joustmania-agent}"

fallback() {
  cat >&2 <<EOF_FALLBACK
agent kill-switch: the flag service did not take the change.
Fallback brake (the game keeps running without the agent):
  docker stop ${AGENT_CONTAINER}
Start it again later with: docker start ${AGENT_CONTAINER}
EOF_FALLBACK
  exit 1
}

case "${1:-status}" in
  on)
    "${FLAGS}" set agent enabled on >/dev/null
    echo "agent kill-switch -> on"
    ;;
  off)
    "${FLAGS}" clear agent enabled || fallback
    echo "agent kill-switch -> $("${FLAGS}" variant agent enabled) (baseline)"
    ;;
  status)
    echo "agent kill-switch = $("${FLAGS}" variant agent enabled)"
    ;;
  *)
    echo "usage: $0 {on|off|status}" >&2
    exit 2
    ;;
esac
