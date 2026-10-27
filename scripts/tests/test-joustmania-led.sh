#!/usr/bin/env bash
# joustmania-led shows the start state on the ACT LED (UC-060 BR-005, A1, A5,
# C-042, FR-183; #2027 PR 4). Runs the helper against a fake sysfs and fake
# state dirs; the real /sys is never touched:
#  - every state writes the C-042 trigger (and delays, trigger first)
#  - ACT is used, led0 is the fallback, no LED at all is one line and exit 0
#  - the board's own trigger is saved once and put back by restore
#  - stop-post: a clean stop restores, a failed or timed-out start is `failed`
#  - from-request applies the state joustmania-status --led left; a missing,
#    invalid, symlinked or FIFO request shows `degraded`, also a request (or
#    start.env) swapped for a symlink between the check and the open (#2135);
#    no request falls back to the start.env result, else restores; starting
#    drops a stale request
#  - a change waits while another holder has the lock (#2135)
#  - the UC-061 hotspot cue (#2137): its own pattern (timer 200/1800 ms), kept
#    apart from the state; it wins over ok, degraded and the board's trigger,
#    loses to starting and failed, comes back after them, and clearing it shows
#    the state (or the board's trigger) again
# Runs as part of `make lint`, or directly: ./scripts/tests/test-joustmania-led.sh
# UC: UC-060 BR-005 A1 A5 Post-S-4
# UC: UC-061 A10 BR-011
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

# The unit's user swaps the request for a symlink after the -f/-L check and
# before the open (#2135): a `timeout` on PATH does the swap just before the
# helper's read runs. The open must refuse the symlink, never read through it.
REAL_TIMEOUT="$(command -v timeout)"
mkdir -p "${WORK}/swap-bin"
cat >"${WORK}/swap-bin/timeout" <<STUB
#!/usr/bin/env bash
# swap <path> for a symlink to <target>, once, then run the real timeout
if [[ -s "\${SWAP_SPEC:-}" ]]; then
  read -r path target <"\$SWAP_SPEC"
  : >"\$SWAP_SPEC"
  rm -f "\$path" && ln -s "\$target" "\$path"
fi
exec "${REAL_TIMEOUT}" "\$@"
STUB
chmod +x "${WORK}/swap-bin/timeout"
led_swap() { PATH="${WORK}/swap-bin:${PATH}" SWAP_SPEC="${CASE}/swap" "$LED" "$@" >"${CASE}/out" 2>&1; }

make_case request-swapped
printf 'ok\n' >"${CASE}/elsewhere"
printf 'failed\n' >"${CASE}/run/led.request"
printf '%s %s\n' "${CASE}/run/led.request" "${CASE}/elsewhere" >"${CASE}/swap"
led_swap from-request
[[ ! -s "${CASE}/swap" ]] || fail "request-swapped: the read did not go through timeout (test stub unused)"
expect "${CASE}/state/state" degraded

make_case start-env-swapped
printf 'phase=start\nresult=ok\n' >"${CASE}/elsewhere"
printf 'phase=start\nresult=failed\n' >"${CASE}/run/start.env"
printf '%s %s\n' "${CASE}/run/start.env" "${CASE}/elsewhere" >"${CASE}/swap"
led starting
led_swap from-request
[[ ! -s "${CASE}/swap" ]] || fail "start-env-swapped: the read did not go through timeout (test stub unused)"
expect "${CASE}/state/state" default
expect "${CASE}/sys/ACT/trigger" mmc0

make_case request-stale
printf 'ok\n' >"${CASE}/run/led.request"
led starting
[[ ! -e "${CASE}/run/led.request" ]] || fail "starting kept a stale request"
led from-request
expect "${CASE}/state/state" default

# --- UC-061 hotspot cue and its precedence (UC-061 A10, BR-011; #2137) ------
# expect_cue: the hotspot pattern is on the LED
expect_cue() {
  expect "${CASE}/sys/ACT/trigger" timer
  expect "${CASE}/sys/ACT/delay_on" 200
  expect "${CASE}/sys/ACT/delay_off" 1800
}
# expect_pattern <state>: the LED shows that UC-060 state, not the cue
expect_pattern() {
  case "$1" in
    ok) expect "${CASE}/sys/ACT/trigger" default-on ;;
    starting) expect "${CASE}/sys/ACT/trigger" heartbeat ;;
    degraded)
      expect "${CASE}/sys/ACT/trigger" timer
      expect "${CASE}/sys/ACT/delay_on" 500
      expect "${CASE}/sys/ACT/delay_off" 500
      ;;
    failed)
      expect "${CASE}/sys/ACT/trigger" timer
      expect "${CASE}/sys/ACT/delay_on" 100
      expect "${CASE}/sys/ACT/delay_off" 100
      ;;
  esac
}

# rank 3 over 4 and 5: the cue covers ok and degraded, clearing it shows them again
for base in ok degraded; do
  make_case "cue-over-${base}"
  led "$base"
  led hotspot-failed || fail "cue-over-${base}: exit $?"
  expect_cue
  expect "${CASE}/state/state" "$base"
  [[ -e "${CASE}/state/hotspot" ]] || fail "cue-over-${base}: cue not recorded"
  led hotspot-clear || fail "cue-over-${base}: clear exit $?"
  expect_pattern "$base"
  [[ ! -e "${CASE}/state/hotspot" ]] || fail "cue-over-${base}: cue still recorded"
  # a state applied while the cue is set records the state and keeps the cue
  led hotspot-failed
  led "$base"
  expect_cue
  expect "${CASE}/state/state" "$base"
done

# rank 3 over 6: nothing set since boot; clearing puts the board's trigger back
make_case cue-no-state
led hotspot-failed
expect_cue
expect "${CASE}/state/default-trigger" mmc0
[[ ! -e "${CASE}/state/state" ]] || fail "cue-no-state: recorded a UC-060 state"
led hotspot-clear
expect "${CASE}/sys/ACT/trigger" mmc0

# a clean stop of the game unit keeps the cue; clearing it then restores the board
make_case cue-clean-stop
led ok
led hotspot-failed
SERVICE_RESULT=success led stop-post
expect_cue
expect "${CASE}/state/state" default
led hotspot-clear
expect "${CASE}/sys/ACT/trigger" mmc0

# ranks 1 and 2 win over the cue: the existing failure cue stays 5 Hz
for top in failed starting; do
  make_case "cue-under-${top}"
  led hotspot-failed
  led "$top"
  expect_pattern "$top"
  expect "${CASE}/state/state" "$top"
  [[ -e "${CASE}/state/hotspot" ]] || fail "cue-under-${top}: the cue record was dropped"
  # the cue set while the higher rank shows changes nothing on the LED
  make_case "cue-set-under-${top}"
  led "$top"
  led hotspot-failed
  expect_pattern "$top"
  [[ -e "${CASE}/state/hotspot" ]] || fail "cue-set-under-${top}: cue not recorded"
  led hotspot-clear
  expect_pattern "$top"
done

# a failed start (ExecStopPost) with the cue set: 5 Hz
make_case cue-stop-failed
led hotspot-failed
led starting
SERVICE_RESULT=exit-code led stop-post
expect_pattern failed

# starting, then ExecStartPost judges ok: the cue comes back
make_case cue-after-start
led hotspot-failed
led starting
expect_pattern starting
printf 'ok\n' >"${CASE}/run/led.request"
led from-request
expect_cue
expect "${CASE}/state/state" ok

# the cue is its own pattern: none of the UC-060 states uses timer 200/1800
for state in starting ok degraded failed; do
  make_case "cue-distinct-${state}"
  led "$state"
  got="$(cat "${CASE}/sys/ACT/trigger") $(cat "${CASE}/sys/ACT/delay_on" 2>/dev/null || true) $(cat "${CASE}/sys/ACT/delay_off" 2>/dev/null || true)"
  [[ "$got" != "timer 200 1800" ]] || fail "cue-distinct-${state}: uses the hotspot pattern"
done

# no LED: one line, exit 0, nothing recorded; clearing without a cue changes nothing
make_case cue-noled none
led hotspot-failed || fail "cue-noled: exit $?"
[[ "$(wc -l <"${CASE}/out")" -eq 1 ]] || fail "cue-noled: want one line, got: $(cat "${CASE}/out")"
[[ ! -e "${CASE}/state/hotspot" ]] || fail "cue-noled: recorded a cue"
make_case cue-clear-none
led ok
led hotspot-clear || fail "cue-clear-none: exit $?"
expect_pattern ok

# --- every change runs under the lock (UC-061 BR-011; #2135) ---------------
# The game unit's hooks and the hotspot timer can run the helper at the same
# moment. While another holder has STATE_DIR/lock, a change must wait: a lock()
# that does nothing would write the LED at once.
if command -v flock >/dev/null 2>&1; then
  make_case lock-held
  led starting
  flock "${CASE}/state/lock" sleep 3 &
  holder=$!
  for _ in $(seq 50); do
    flock -n "${CASE}/state/lock" true || break
    sleep 0.05
  done
  led ok &
  writer=$!
  sleep 1
  expect "${CASE}/sys/ACT/trigger" heartbeat
  expect "${CASE}/state/state" starting
  wait "$holder" || true
  wait "$writer" || fail "lock-held: the waiting writer failed"
  expect "${CASE}/sys/ACT/trigger" default-on
  expect "${CASE}/state/state" ok
else
  echo "SKIP lock-held: no flock on this host"
fi

if [[ $fails -gt 0 ]]; then
  echo "test-joustmania-led: ${fails} failure(s)"
  exit 1
fi
echo "test-joustmania-led: OK"
