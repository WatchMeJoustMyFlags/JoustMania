#!/usr/bin/env bash
# Enable the experiment cohort loop for a `make dry-run` (#1077; epic #1407).
#
# The LIVE gate of the cohort loop is the `experiments_enabled` flag (agent set);
# it and the master `enabled` kill-switch are fail-closed `off` in the baseline.
# This helper flips both through the flag service (scripts/flags.sh):
#   on   -> `enabled` and `experiments_enabled` overlays set to on
#   off  -> both overlays cleared: the fail-closed baseline returns
# Nothing is persisted (re-review N4). The in-game decision backend is NOT touched
# here: use ./scripts/agent-backend.sh (decision.backend, #1232).
#
# Usage:
#   ./scripts/agent-dryrun-enable.sh on
#   ./scripts/agent-dryrun-enable.sh off
#   ./scripts/agent-dryrun-enable.sh status
#
# The flips take effect LIVE (no agent restart). A restart is only needed for the
# AGENT_INFERENCE_BASE_URL / _MODEL env values and the ANTHROPIC_API_KEY (read at
# start); the text backend itself is the inference.backend flag (live, #1810).
#
# Env: FLAG_SERVICE_URL (default http://localhost/flags, the envoy route).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAGS="${FLAGS_SH:-${SCRIPT_DIR}/flags.sh}"
export FLAGS_WRITER="${FLAGS_WRITER:-operator}"

case "${1:-status}" in
  on)
    for flag in enabled experiments_enabled; do
      "${FLAGS}" set agent "${flag}" on >/dev/null
      echo "  ${flag} -> on"
    done
    echo "dry-run experiment gate -> on"
    ;;
  off)
    for flag in enabled experiments_enabled; do
      "${FLAGS}" clear agent "${flag}"
      echo "  ${flag} -> $("${FLAGS}" variant agent "${flag}") (baseline)"
    done
    echo "dry-run experiment gate -> off"
    ;;
  status)
    for flag in enabled experiments_enabled decision.backend; do
      echo "${flag} = $("${FLAGS}" variant agent "${flag}")"
    done
    ;;
  *)
    echo "usage: $0 {on|off|status}" >&2
    exit 2
    ;;
esac
