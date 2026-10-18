#!/usr/bin/env bash
# Self-contained tests for the change-detection mapping of
# scripts/ci/local-gates.sh (`make pr-gates`). Uses --plan, so no gate runs.
#
# Runs as part of `make lint`, or directly:
#   ./scripts/tests/test-local-gates.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATES="${SCRIPT_DIR}/../ci/local-gates.sh"
# Inputs may leak in from an outer `make pr-gates SERVICE=...` run.
unset SERVICE PR_GATES_FILES BASE PR_LABELS
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0

# expect "<changed files, space separated>" "<expected gates, space separated>"
expect() {
  tr ' ' '\n' <<<"$1" >"${WORK}/files"
  local got
  got="$(PR_GATES_FILES="${WORK}/files" "${GATES}" --plan | tr '\n' ' ' | sed 's/ $//')"
  if [[ "$got" != "$2" ]]; then
    echo "FAIL: files [$1]" >&2
    echo "  want: $2" >&2
    echo "  got:  $got" >&2
    exit 1
  fi
  pass=$((pass + 1))
}

ALWAYS="lint spec-lint spec-touched"
expect "docs/CONTRIBUTING.md" "$ALWAYS"
expect "services/menu/servicer.py" "$ALWAYS py:menu"
expect "services/menu/metrics.py" "$ALWAYS py:menu contract"
expect "services/pairing_daemon/daemon.py services/python_hid/x.py" "$ALWAYS py:pairing_daemon py:python_hid"
expect "lib/feature_flags.py" \
  "$ALWAYS py:lib py:game_coordinator py:menu py:controller_manager py:audio py:pairing_daemon py:python_hid"
expect "proto/menu.proto" \
  "$ALWAYS py:lib py:game_coordinator py:menu py:controller_manager py:audio py:pairing_daemon py:python_hid connect-proxy"
expect "services/agent/decision/rules.go" "$ALWAYS go:agent"
expect "services/flag-service/registry.yaml" "$ALWAYS go:flag-service contract-flagd yaml-dupkeys"
expect "services/flagd/game.json" "$ALWAYS go:agent go:flag-service contract-flagd"
expect "services/connect-proxy/main.go" "$ALWAYS connect-proxy"
expect "services/dashboard/src/App.tsx" "$ALWAYS dashboard contract"
expect "services/dashboard/package.json" "$ALWAYS dashboard"
expect "services/rust-hid/src/main.rs" "$ALWAYS rust-hid"
expect "services/grafana/dashboards/game.json" "$ALWAYS contract grafana-json"
expect "services/otel-collector/config.yaml" "$ALWAYS contract yaml-dupkeys"
expect "docs/specs/telemetry.md" "$ALWAYS contract"
expect "docker-compose.yml" "$ALWAYS contract compose yaml-dupkeys"
expect "docker-compose.lite.yml" "$ALWAYS compose yaml-dupkeys"

# Explicit SERVICE keys replace auto-detection (hyphen/underscore agnostic).
got="$(SERVICE="flag_service,game-coordinator" PR_GATES_FILES=/dev/null "${GATES}" --plan | tr '\n' ' ')"
[[ "$got" == "lint spec-lint py:game_coordinator go:flag-service " ]] || { echo "FAIL: SERVICE keys: $got" >&2; exit 1; }
got="$(PR_GATES_FILES=/dev/null "${GATES}" --plan menu agent | tr '\n' ' ')"
[[ "$got" == "lint spec-lint py:menu go:agent " ]] || { echo "FAIL: positional keys: $got" >&2; exit 1; }
if PR_GATES_FILES=/dev/null "${GATES}" --plan nonsense 2>/dev/null; then
  echo "FAIL: unknown SERVICE key must exit non-zero" >&2; exit 1
fi
pass=$((pass + 3))

echo "test-local-gates: ${pass} cases passed"
