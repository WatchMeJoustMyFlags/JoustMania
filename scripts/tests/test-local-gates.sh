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
  got="$(PR_GATES_FILES="${WORK}/files" "${GATES}" --plan 2>/dev/null | tr '\n' ' ' | sed 's/ $//')"
  if [[ "$got" != "$2" ]]; then
    echo "FAIL: files [$1]" >&2
    echo "  want: $2" >&2
    echo "  got:  $got" >&2
    exit 1
  fi
  pass=$((pass + 1))
}

ALWAYS="lint spec-lint spec-touched"
PY_ALL="py:game_coordinator py:menu py:controller_manager py:audio py:pairing_daemon py:python_hid py:operator_pad"
# Each case mirrors a `changes` filter in .github/workflows/ci.yml.
expect "docs/CONTRIBUTING.md" "$ALWAYS"
expect "services/menu/servicer.py" "$ALWAYS py:menu contract"
expect "services/pairing_daemon/daemon.py services/python_hid/x.py" "$ALWAYS py:pairing_daemon py:python_hid contract"
expect "services/operator_pad/gestures.py" "$ALWAYS py:operator_pad contract"
# lib: shared_python + lib + telemetry + flagd_contract
expect "lib/feature_flags.py" "$ALWAYS py:lib $PY_ALL contract contract-flagd"
# root pyproject.toml is shared_python only (not the lib filter)
expect "pyproject.toml" "$ALWAYS $PY_ALL service"
expect "proto/menu.proto" "$ALWAYS py:lib $PY_ALL connect-proxy agent-protos contract service"
# rust filter includes the HID proto
expect "proto/psmove_hid.proto" "$ALWAYS py:lib $PY_ALL connect-proxy agent-protos rust-hid contract service"
expect "services/agent/decision/rules.go" "$ALWAYS go:agent contract contract-flagd"
# proto_pkg (#1801): the committed agent Go stubs and their generator
expect "services/agent/gen/game_coordinator/game_coordinator.pb.go" "$ALWAYS go:agent agent-protos contract contract-flagd"
expect "scripts/ci/gen-agent-protos.sh" "$ALWAYS agent-protos contract"
# lib filter pins one agent file and one service-tier test
expect "services/agent/experiment/targeting.go" "$ALWAYS py:lib go:agent contract contract-flagd"
expect "tests/service/test_experiment_shadow_isolation.py" "$ALWAYS py:lib service-touched"
# agent_tests reads the registry; flagd_contract does not include it
expect "services/flag-service/registry.yaml" "$ALWAYS go:agent go:flag-service contract service yaml-dupkeys"
expect "services/flag-service/main.go" "$ALWAYS go:flag-service contract service"
expect "docs/specs/flags.md" "$ALWAYS go:flag-service contract"
# flagd files: lib, agent_tests, flag_service_tests, telemetry, flagd_contract
expect "services/flagd/game.json" "$ALWAYS py:lib go:agent go:flag-service contract contract-flagd service"
expect "services/game_coordinator/servicer.py" "$ALWAYS py:game_coordinator contract contract-flagd"
expect "Makefile" "$ALWAYS contract contract-flagd"
expect "tests/service/conftest.py" "$ALWAYS service"
expect "tests/integration/helpers.py" "$ALWAYS service"
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
expect "docker-compose.yml" "$ALWAYS contract contract-flagd service compose yaml-dupkeys"
expect "docker-compose.lite.yml" "$ALWAYS compose yaml-dupkeys"

# Service tier (#1839): full tier only for harness / stack changes, targeted
# files for edited test_*.py, nothing locally for product paths (PR CI runs it).
expect "tests/service/harness/stack.py" "$ALWAYS service"
expect "tests/service/lobby_flow.py" "$ALWAYS service"
expect "tests/service/test_tc009_kill_switch.py" "$ALWAYS service-touched"
expect "tests/service/harness/stack.py tests/service/test_tc009_kill_switch.py" "$ALWAYS service"
expect "tests/service/test_deleted_in_this_diff.py" "$ALWAYS"
expect "tests/service/README.md" "$ALWAYS"
expect "services/menu/server.py" "$ALWAYS py:menu contract service"
expect "services/agent/main.go" "$ALWAYS go:agent contract contract-flagd"
expect "tests/integration/fake_openai/server.py" "$ALWAYS service"

# Explicit SERVICE keys replace auto-detection (hyphen/underscore agnostic).
got="$(SERVICE="flag_service,game-coordinator" PR_GATES_FILES=/dev/null "${GATES}" --plan 2>/dev/null | tr '\n' ' ')"
[[ "$got" == "lint spec-lint py:game_coordinator go:flag-service " ]] || { echo "FAIL: SERVICE keys: $got" >&2; exit 1; }
got="$(PR_GATES_FILES=/dev/null "${GATES}" --plan menu agent | tr '\n' ' ')"
[[ "$got" == "lint spec-lint py:menu go:agent " ]] || { echo "FAIL: positional keys: $got" >&2; exit 1; }
if PR_GATES_FILES=/dev/null "${GATES}" --plan nonsense 2>/dev/null; then
  echo "FAIL: unknown SERVICE key must exit non-zero" >&2; exit 1
fi
echo tests/service/test_harness.py >"${WORK}/files"
got="$(SERVICE=service-touched PR_GATES_FILES="${WORK}/files" "${GATES}" --plan 2>/dev/null | tr '\n' ' ')"
[[ "$got" == "lint spec-lint service-touched " ]] || { echo "FAIL: SERVICE=service-touched: $got" >&2; exit 1; }
got="$(SERVICE=menu PR_GATES_FILES="${WORK}/files" "${GATES}" --plan 2>/dev/null | tr '\n' ' ')"
[[ "$got" == "lint spec-lint py:menu " ]] || { echo "FAIL: SERVICE=menu must not add service-touched: $got" >&2; exit 1; }
pass=$((pass + 5))

# Note: printed whenever the CI service filter matched and the full tier did not run.
note='note: service tier not run locally; PR CI runs the full tier'
check_note() {  # <files> <want: yes|no>
  tr ' ' '\n' <<<"$1" >"${WORK}/files"
  local err
  err="$(PR_GATES_FILES="${WORK}/files" "${GATES}" --plan 2>&1 >/dev/null)"
  if [[ ("$2" == yes && "$err" != "$note") || ("$2" == no && -n "$err") ]]; then
    echo "FAIL: note for [$1] (want $2): $err" >&2; exit 1
  fi
  pass=$((pass + 1))
}
check_note "lib/feature_flags.py" yes
check_note "lib/feature_flags.py tests/service/test_tc009_kill_switch.py" yes
check_note "tests/service/test_tc009_kill_switch.py" yes
check_note "tests/service/harness/stack.py" no
check_note "docs/README.md" no

# service-touched without changed test files: skipped, never an empty SERVICE_TESTS.
if PR_GATES_FILES=/dev/null "${GATES}" --print-cmd service-touched 2>/dev/null; then
  echo "FAIL: service-touched with no files must not print a command" >&2; exit 1
fi
got="$(SERVICE=service-touched PR_GATES_FILES=/dev/null "${GATES}" --plan 2>/dev/null | tr '\n' ' ')"
[[ "$got" == "lint spec-lint " ]] || { echo "FAIL: empty service-touched must be skipped: $got" >&2; exit 1; }
if "${GATES}" --print-cmd 2>/dev/null; then echo "FAIL: --print-cmd without argument" >&2; exit 1; fi
if PR_GATES_FILES=/dev/null "${GATES}" --print-cmd nonsense 2>/dev/null; then echo "FAIL: --print-cmd unknown gate" >&2; exit 1; fi
pass=$((pass + 4))

# Command level: the targeted lane passes the files through SERVICE_TESTS.
printf '%s\n' tests/service/test_tc009_kill_switch.py tests/service/test_harness.py >"${WORK}/files"
got="$(PR_GATES_FILES="${WORK}/files" "${GATES}" --print-cmd service-touched)"
want="make test-service SERVICE_TESTS='tests/service/test_harness.py tests/service/test_tc009_kill_switch.py'"
[[ "$got" == "$want" ]] || { echo "FAIL: service-touched command: $got" >&2; exit 1; }
printf '%s\n' tests/service/harness/stack.py >"${WORK}/files"
got="$(PR_GATES_FILES="${WORK}/files" "${GATES}" --print-cmd service)"
[[ "$got" == "make test-service" ]] || { echo "FAIL: service command: $got" >&2; exit 1; }
pass=$((pass + 2))

echo "test-local-gates: ${pass} cases passed"
