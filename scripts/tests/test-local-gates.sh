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
PY_ALL="py:game_coordinator py:menu py:controller_manager py:audio py:pairing_daemon py:python_hid"
# Each case mirrors a `changes` filter in .github/workflows/ci.yml.
expect "docs/CONTRIBUTING.md" "$ALWAYS"
expect "services/menu/servicer.py" "$ALWAYS py:menu contract"
expect "services/pairing_daemon/daemon.py services/python_hid/x.py" "$ALWAYS py:pairing_daemon py:python_hid contract"
# lib: shared_python + lib + telemetry + flagd_contract
expect "lib/feature_flags.py" "$ALWAYS py:lib $PY_ALL contract contract-flagd"
# root pyproject.toml is shared_python only (not the lib filter)
expect "pyproject.toml" "$ALWAYS $PY_ALL"
expect "proto/menu.proto" "$ALWAYS py:lib $PY_ALL connect-proxy contract"
# rust filter includes the HID proto
expect "proto/psmove_hid.proto" "$ALWAYS py:lib $PY_ALL connect-proxy rust-hid contract"
expect "services/agent/decision/rules.go" "$ALWAYS go:agent contract contract-flagd"
# lib filter pins one agent file and one integration test
expect "services/agent/experiment/targeting.go" "$ALWAYS py:lib go:agent contract contract-flagd"
expect "tests/integration/test_experiment_shadow_isolation.py" "$ALWAYS py:lib"
# agent_tests reads the registry; flagd_contract does not include it
expect "services/flag-service/registry.yaml" "$ALWAYS go:agent go:flag-service contract yaml-dupkeys"
expect "services/flag-service/main.go" "$ALWAYS go:flag-service contract"
expect "docs/specs/flags.md" "$ALWAYS go:flag-service contract"
# flagd files: lib, agent_tests, flag_service_tests, telemetry, flagd_contract
expect "services/flagd/game.json" "$ALWAYS py:lib go:agent go:flag-service contract contract-flagd"
expect "services/game_coordinator/servicer.py" "$ALWAYS py:game_coordinator contract contract-flagd"
expect "Makefile" "$ALWAYS contract contract-flagd"
expect ".github/workflows/ci.yml" "$ALWAYS contract-flagd yaml-dupkeys"
expect "tests/contract/test_x.py" "$ALWAYS contract contract-flagd"
expect "scripts/ci/safe-merge.sh" "$ALWAYS contract"
expect "services/connect-proxy/main.go" "$ALWAYS connect-proxy contract"
expect "services/dashboard/src/App.tsx" "$ALWAYS dashboard contract"
expect "services/rust-hid/src/main.rs" "$ALWAYS rust-hid contract"
expect "services/grafana/dashboards/game.json" "$ALWAYS contract grafana-json"
expect "services/otel-collector/config.yaml" "$ALWAYS contract yaml-dupkeys"
expect "docs/specs/telemetry.md" "$ALWAYS contract"
# every docs/specs file feeds `make specs-check` (generated tables, notes sidecars)
expect "docs/specs/game.md" "$ALWAYS contract"
expect "docs/specs/notes/grpc-rpcs.yaml" "$ALWAYS contract yaml-dupkeys"
expect "docs/README.md" "$ALWAYS"
expect "docker-compose.yml" "$ALWAYS contract contract-flagd compose yaml-dupkeys"
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
