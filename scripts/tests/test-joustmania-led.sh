#!/usr/bin/env bash
# joustmania-led shows the start state on the ACT LED (UC-060 BR-005, A1, A5,
# C-042, FR-183; #2027 PR 4). Runs the helper against a fake sysfs and fake
# state dirs; the real /sys is never touched:
#  - every state writes the C-042 trigger (and delays, trigger first)
#  - ACT is used, led0 is the fallback, no LED at all is one line and exit 0
#  - the board's own trigger is saved once and put back by restore
#  - stop-post: a clean stop restores, a failed or timed-out start is `failed`
#  - from-request applies the state joustmania-status --led left; a missing,
#    invalid, symlinked or FIFO request shows `degraded`; no request falls back
#    to the start.env result, else restores; starting drops a stale request
# Runs as part of `make lint`, or directly: ./scripts/tests/test-joustmania-led.sh
# UC: UC-060 BR-005 A1 A5 Post-S-4
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LED="${ROOT}/scripts/setup/joustmania-led.sh"
unset SERVICE_RESULT EXIT_CODE EXIT_STATUS || true
# A test that reaches for the real docker fails instead of using it
export PATH="${ROOT}/scripts/tests/lib/fail-docker:${PATH}"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# make_case <name> [led name]: a fake sysfs with one LED whose trigger is mmc0
make_case() {
  CASE="${WORK}/$1"
  local name="${2:-ACT}"
  mkdir -p "${CASE}/sys" "${CASE}/state" "${CASE}/run"
  if [[ "$name" != none ]]; then
    mkdir -p "${CASE}/sys/${name}"
    printf 'none timer heartbeat default-on [mmc0] mmc1\n' >"${CASE}/sys/${name}/trigger"
  fi
  export JOUSTMANIA_LED_SYSFS="${CASE}/sys" JOUSTMANIA_LED_STATE_DIR="${CASE}/state" \
    JOUSTMANIA_RUN_STATE_DIR="${CASE}/run"
}
led() { "$LED" "$@" >"${CASE}/out" 2>&1; }
# expect <file> <content>
expect() {
  local got
  got="$(cat "$1" 2>/dev/null || echo '<missing>')"
  [[ "$got" == "$2" ]] || fail "${CASE##*/}: ${1#"${CASE}"/} is '$got', want '$2'"
}

# --- every state maps to its sysfs writes (C-042) ---------------------------
for row in "starting heartbeat - -" "ok default-on - -" "degraded timer 500 500" "failed timer 100 100"; do
  read -r state trigger on off <<<"$row"
  make_case "state-${state}"
  led "$state" || fail "$state: exit $?"
  expect "${CASE}/sys/ACT/trigger" "$trigger"
  if [[ "$on" == - ]]; then
    [[ ! -e "${CASE}/sys/ACT/delay_on" ]] || fail "$state: wrote delay_on"
  else
    expect "${CASE}/sys/ACT/delay_on" "$on"
    expect "${CASE}/sys/ACT/delay_off" "$off"
  fi
  expect "${CASE}/state/state" "$state"
  expect "${CASE}/state/default-trigger" mmc0
done

# --- led0 fallback, no LED, unknown argument --------------------------------
make_case led0 led0
led failed
expect "${CASE}/sys/led0/trigger" timer
expect "${CASE}/sys/led0/delay_on" 100

make_case noled none
led failed || fail "no LED: exit $?"
[[ "$(wc -l <"${CASE}/out")" -eq 1 ]] || fail "no LED: want one line, got: $(cat "${CASE}/out")"
grep -q "no writable ACT/led0 LED" "${CASE}/out" || fail "no LED: line not found"
[[ ! -e "${CASE}/state/state" ]] || fail "no LED: recorded a state"

make_case readonly
chmod a-w "${CASE}/sys/ACT/trigger"
if [[ -w "${CASE}/sys/ACT/trigger" ]]; then
  echo "SKIP readonly (running as root)"
else
  led ok || fail "no permission: exit $?"
  [[ ! -e "${CASE}/state/state" ]] || fail "no permission: recorded a state"
fi

make_case usage
if led bogus; then fail "unknown argument exits 0"; fi

# --- restore: the board's own trigger, saved once ---------------------------
make_case restore
led starting
led degraded
led restore
expect "${CASE}/sys/ACT/trigger" mmc0
expect "${CASE}/state/state" default

make_case restore-untouched
led restore
expect "${CASE}/sys/ACT/trigger" 'none timer heartbeat default-on [mmc0] mmc1'
grep -q "nothing to restore" "${CASE}/out" || fail "restore-untouched: no note"

# --- stop-post: clean stop restores, a failed start is failed (A5) ----------
make_case stop-clean
led starting
SERVICE_RESULT=success led stop-post
expect "${CASE}/sys/ACT/trigger" mmc0

for result in exit-code timeout signal; do
  make_case "stop-${result}"
  led starting
  SERVICE_RESULT="$result" EXIT_CODE=exited EXIT_STATUS=1 led stop-post
  expect "${CASE}/sys/ACT/trigger" timer
  expect "${CASE}/sys/ACT/delay_on" 100
  expect "${CASE}/state/state" failed
done

make_case stop-unset
led stop-post
expect "${CASE}/state/state" failed

# --- from-request (ExecStartPost) -------------------------------------------
for state in ok degraded failed; do
  make_case "request-${state}"
  printf '%s\n' "$state" >"${CASE}/run/led.request"
  led from-request
  expect "${CASE}/state/state" "$state"
done

# no request (a status without --led, after a rollback): start.env result, else restore
for result in ok degraded; do
  make_case "request-missing-${result}"
  printf 'phase=start\nresult=%s\n' "$result" >"${CASE}/run/start.env"
  led from-request
  expect "${CASE}/state/state" "$result"
  grep -q "no led.request" "${CASE}/out" || fail "request-missing-${result}: no note"
done
make_case request-missing-nothing
led starting
led from-request
expect "${CASE}/sys/ACT/trigger" mmc0
expect "${CASE}/state/state" default

make_case request-fifo
mkfifo "${CASE}/run/led.request"
started=$SECONDS
led from-request
((SECONDS - started < 10)) || fail "request-fifo: hung on a FIFO"
expect "${CASE}/state/state" degraded

make_case request-invalid
printf 'ok; rm -rf /\n' >"${CASE}/run/led.request"
led from-request
expect "${CASE}/state/state" degraded
grep -q "rm -rf" "${CASE}/out" && fail "request-invalid: logged the file content"

make_case request-symlink
printf 'ok\n' >"${CASE}/elsewhere"
ln -s "${CASE}/elsewhere" "${CASE}/run/led.request"
led from-request
expect "${CASE}/state/state" degraded

make_case request-stale
printf 'ok\n' >"${CASE}/run/led.request"
led starting
[[ ! -e "${CASE}/run/led.request" ]] || fail "starting kept a stale request"
led from-request
expect "${CASE}/state/state" default

if [[ $fails -gt 0 ]]; then
  echo "test-joustmania-led: ${fails} failure(s)"
  exit 1
fi
echo "test-joustmania-led: OK"
