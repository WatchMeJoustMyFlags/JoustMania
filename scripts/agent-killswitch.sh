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
# Env: FLAG_SERVICE_URL (default http://localhost/flags, the envoy route).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAGS="${FLAGS_SH:-${SCRIPT_DIR}/flags.sh}"
export FLAGS_WRITER="${FLAGS_WRITER:-operator}"

case "${1:-status}" in
  on)
    "${FLAGS}" set agent enabled on >/dev/null
    echo "agent kill-switch -> on"
    ;;
  off)
    "${FLAGS}" clear agent enabled
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
