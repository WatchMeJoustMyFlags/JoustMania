#!/usr/bin/env bash
# scripts/hardware/reset_bluetooth_connections.sh (UC-005 A9): removes the PS Move
# registrations through the pairing-daemon container, restarts the daemon after a
# real run (not after --dry-run, not after a one-off container), and exits with the
# registration reset's status.
#
# The script is copied into a scratch repo next to a fake scripts/setup/joustmania-compose.sh
# that only logs its arguments, so no docker, bluetoothctl or systemctl is touched.
# Runs as part of `make lint`, or directly: ./scripts/tests/test-reset-bluetooth-connections.sh
# UC: UC-005 A9
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unset COMPOSE DOCKER COMPOSE_PROJECT_NAME COMPOSE_PROFILES || true
# A test that reaches for the real docker fails instead of using it

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
REPO="${WORK}/repo"
mkdir -p "${REPO}/scripts/hardware" "${REPO}/scripts/setup"
cp "${ROOT}/scripts/hardware/reset_bluetooth_connections.sh" "${REPO}/scripts/hardware/"
LOG="${WORK}/compose.log"

# Fake compose: RUNNING=1 lists pairing-daemon as running; EXEC_RC is the exit code of exec/run.
cat >"${REPO}/scripts/setup/joustmania-compose.sh" <<'FAKE'
#!/usr/bin/env bash
echo "$*" >>"${COMPOSE_LOG}"
case "$1" in
  ps) [[ "${RUNNING:-1}" == 1 ]] && echo pairing-daemon; exit 0 ;;
  exec | run) exit "${EXEC_RC:-0}" ;;
  restart) exit 0 ;;
esac
exit 0
FAKE
chmod +x "${REPO}/scripts/setup/joustmania-compose.sh" "${REPO}/scripts/hardware/reset_bluetooth_connections.sh"

fails=0
run_case() { # run_case <name> <running> <exec_rc> <want_rc> <want_log_regex> <unwanted_log_regex> [args...]
  local name="$1" running="$2" exec_rc="$3" want_rc="$4" want="$5" unwanted="$6"
  shift 6
  : >"${LOG}"
  local rc=0
  RUNNING="${running}" EXEC_RC="${exec_rc}" COMPOSE_LOG="${LOG}" \
    "${REPO}/scripts/hardware/reset_bluetooth_connections.sh" "$@" >"${WORK}/out" 2>&1 || rc=$?
  if [[ "${rc}" -ne "${want_rc}" ]]; then
    echo "FAIL ${name}: exit ${rc}, want ${want_rc}"; cat "${WORK}/out"; fails=$((fails + 1)); return
  fi
  if ! grep -qE "${want}" "${LOG}"; then
    echo "FAIL ${name}: compose log lacks /${want}/:"; sed 's/^/    /' "${LOG}"; fails=$((fails + 1)); return
  fi
  if [[ -n "${unwanted}" ]] && grep -qE "${unwanted}" "${LOG}"; then
    echo "FAIL ${name}: compose log has /${unwanted}/:"; sed 's/^/    /' "${LOG}"; fails=$((fails + 1)); return
  fi
  echo "ok   - ${name}"
}

run_case "real run removes registrations in the daemon, then restarts it" 1 0 0 \
  '^restart pairing-daemon$' ''
grep -qE '^exec -T pairing-daemon python -m psmove_pairing.registrations$' "${LOG}" \
  || { echo "FAIL real run: no exec of the registration reset"; fails=$((fails + 1)); }
restart_line="$(grep -n '^restart' "${LOG}" | head -1 | cut -d: -f1)"
exec_line="$(grep -n '^exec' "${LOG}" | head -1 | cut -d: -f1)"
[[ -n "${restart_line}" && -n "${exec_line}" && "${exec_line}" -lt "${restart_line}" ]] \
  || { echo "FAIL real run: restart must follow the reset"; fails=$((fails + 1)); }

run_case "--dry-run lists only and does not restart the daemon" 1 0 0 \
  '^exec -T pairing-daemon python -m psmove_pairing.registrations --dry-run$' '^restart' --dry-run

run_case "a failed removal still restarts the daemon and exits non-zero" 1 3 3 \
  '^restart pairing-daemon$' ''

run_case "daemon not running: one-off container, no restart" 0 0 0 \
  '^run --rm --no-deps -T pairing-daemon python -m psmove_pairing.registrations$' '^(restart|exec)'

run_case "daemon not running and --dry-run: one-off container with the flag" 0 0 0 \
  '^run --rm --no-deps -T pairing-daemon python -m psmove_pairing.registrations --dry-run$' '^(restart|exec)' --dry-run

if [[ "${fails}" -gt 0 ]]; then echo "${fails} failure(s)"; exit 1; fi
echo "all reset_bluetooth_connections cases passed"
