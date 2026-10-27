#!/usr/bin/env bash
# joustmania-status reports the Pi's state (UC-060 BR-005, A4, A5, BR-006,
# FR-183; #2027 PR 2b). Runs the command against a stub docker, systemctl and
# timedatectl, in a real git checkout pinned at joustmania-v9.9.9:
#  - healthy: every check OK, `state: ok`, exit 0, the pinned version shown
#  - start-limit-hit: FAIL with the recovery command, exit 2
#  - a game image missing: FAIL naming it, the exact joustmania-upgrade.sh
#    command, exit 2
#  - an observability service exited: WARN, `state: degraded`, exit 1
#  - a game service running but unhealthy (rust-hid wedged, #2125): FAIL with
#    `docker restart <container>`, exit 2; an unhealthy observability service: WARN
#  - the clock not NTP synchronised: INFO only (no RTC, accepted), `state: ok`
#  - docker not reachable: FAIL "not reachable", no image reported missing
#  - the unit still activating, a game service not running yet: `state: starting`, exit 1
#  - a branch checkout: WARN not pinned (A4)
#  - no env value is printed
#  - the ACT LED line (C-042): the recorded state and the live trigger; a stale
#    installed helper is named
#  - git cannot read the checkout: WARN naming that (not a branch), degraded (A4)
#  - a start refused on a branch checkout (A4): the reason from start.env is
#    shown on the last-start line, exit 2
#  - --led (ExecStartPost, #2027 PR 4): the unit still activating counts as
#    started, the state (never `starting`) is written to led.request
# Runs as part of `make lint`, or directly: ./scripts/tests/test-joustmania-status.sh
# UC: UC-060 BR-005 A1 A4 A5 BR-006; UC-002 A7
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unset COMPOSE_PROJECT_NAME COMPOSE_PROFILES IMAGE_TAG RUNTIME_DIRECTORY JOUSTMANIA_STATUS_DIR || true
# A test that reaches for the real docker fails instead of using it
export PATH="${ROOT}/scripts/tests/lib/fail-docker:${PATH}"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
SERVICES="flagd rust-hid game-coordinator menu jaeger"
SECRET="dummy-secret-status-2027"

# make_case <name>: ${WORK}/<name>/{repo,bin}; sets CASE
#   $CASE/missing: images not on the Pi; $CASE/exited: services not running;
#   $CASE/unhealthy: services running but unhealthy
#   $CASE/active, $CASE/result: the unit's ActiveState / Result; $CASE/ntp
make_case() {
  CASE="${WORK}/$1"
  mkdir -p "${CASE}/repo/scripts/setup" "${CASE}/bin" "${CASE}/status" "${CASE}/run" "${CASE}/ledstate" "${CASE}/sys"
  cp "${ROOT}/scripts/setup/joustmania-status.sh" "${ROOT}/scripts/setup/joustmania-led.sh" "${ROOT}/scripts/setup/joustmania-compose.sh" \
    "${ROOT}/scripts/setup/lib-joustmania.sh" "${CASE}/repo/scripts/setup/"
  printf 'IMAGE_TAG=0.0.0-test\n' >"${CASE}/repo/.env"
  printf 'JEV_API_KEY=%s\n' "${SECRET}" >"${CASE}/repo/.env.local"
  git -C "${CASE}/repo" init -q
  git -C "${CASE}/repo" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q --allow-empty -m pin
  git -C "${CASE}/repo" -c user.name=t -c user.email=t@t -c tag.gpgsign=false tag joustmania-v9.9.9
  git -C "${CASE}/repo" checkout -q --detach
  : >"${CASE}/missing"
  : >"${CASE}/exited"
  : >"${CASE}/unhealthy"
  echo active >"${CASE}/active"
  echo success >"${CASE}/result"
  echo yes >"${CASE}/ntp"
  printf 'phase=start\nresult=ok\nmissing=\ndown=\nts=2026-10-26T10:00:00+01:00\n' >"${CASE}/status/start.env"
  cat >"${CASE}/bin/docker" <<STUB
#!/usr/bin/env bash
C="${CASE}"
[[ "\$1" == info ]] && { [[ -e "\$C/info-fails" ]] && exit 1; exit 0; }
case " \$* " in
  *" compose "*" config "*)
    printf 'services:\n'
    for s in ${SERVICES}; do printf '  %s:\n    environment:\n      JEV_API_KEY: ${SECRET}\n    image: i/%s:9.9.9\n' "\$s" "\$s"; done
    exit 0 ;;
  *" compose "*" ps "*)
    for s in ${SERVICES}; do
      if grep -qxF "\$s" "\$C/exited"; then echo "\$s|exited||joustmania-\$s"
      elif grep -qxF "\$s" "\$C/unhealthy"; then echo "\$s|running|unhealthy|joustmania-\$s"
      else echo "\$s|running|healthy|joustmania-\$s"; fi
    done; exit 0 ;;
  " image inspect "*) grep -qxF "\$3" "\$C/missing" && exit 1; exit 0 ;;
esac
exit 0
STUB
  cat >"${CASE}/bin/systemctl" <<STUB
#!/usr/bin/env bash
case " \$* " in
  *" ActiveState "*) cat "${CASE}/active" ;;
  *" SubState "*) echo exited ;;
  *" Result "*) cat "${CASE}/result" ;;
esac
STUB
  printf '#!/usr/bin/env bash\ncat "%s/ntp"\n' "${CASE}" >"${CASE}/bin/timedatectl"
  chmod +x "${CASE}/bin/"*
}

# run_status: returns the command's exit code; output in $CASE/out.log
run_status() {
  env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_STATUS_DIR="${CASE}/status" \
    JOUSTMANIA_RUN_STATE_DIR="${CASE}/run" JOUSTMANIA_LED_STATE_DIR="${CASE}/ledstate" \
    JOUSTMANIA_LED_SYSFS="${CASE}/sys" JOUSTMANIA_LED_HELPER="${CASE}/helper" \
    bash "${CASE}/repo/scripts/setup/joustmania-status.sh" "$@" >"${CASE}/out.log" 2>&1
}
expect() { # $1 = case, $2 = expected exit, $3 = expected state, $4.. = status args
  local rc=0
  run_status "${@:4}" || rc=$?
  [[ "${rc}" == "$2" ]] || fail "$1: exit ${rc}, expected $2: $(cat "${CASE}/out.log")"
  [[ "$(tail -n 1 "${CASE}/out.log")" == "state: $3" ]] || fail "$1: last line is not 'state: $3': $(tail -n 1 "${CASE}/out.log")"
  grep -q "${SECRET}" "${CASE}/out.log" && fail "$1: an env value was printed"
  return 0
}

# --- healthy ----------------------------------------------------------------
make_case healthy
expect healthy 0 ok
grep -qx 'OK   version: joustmania-v9.9.9 (image tag 9.9.9)' "${CASE}/out.log" || fail "healthy: pinned version not shown"
grep -qx 'OK   unit: active (exited)' "${CASE}/out.log" || fail "healthy: unit state not shown"
grep -qx 'OK   service menu: running (healthy)' "${CASE}/out.log" || fail "healthy: per-service health not shown"
grep -qx 'OK   images: all present' "${CASE}/out.log" || fail "healthy: images not checked"
grep -qx 'OK   clock: NTP synchronised' "${CASE}/out.log" || fail "healthy: clock not checked"
grep -q '^INFO last start: result=ok ts=' "${CASE}/out.log" || fail "healthy: last start not shown"
grep -qx 'INFO led: not set since boot' "${CASE}/out.log" || fail "led: no 'not set since boot' line: $(grep 'led' "${CASE}/out.log")"
[[ ! -e "${CASE}/run/led.request" ]] || fail "led: a plain status wrote led.request"
grep -q '^INFO led helper: .* not installed' "${CASE}/out.log" || fail "led: a missing helper is not named"

# --- start-limit-hit (A5, BR-006, #2102) -------------------------------------
make_case start-limit
echo failed >"${CASE}/active"
echo start-limit-hit >"${CASE}/result"
expect start-limit 2 failed
grep -qF 'FAIL unit: start-limit-hit' "${CASE}/out.log" || fail "start-limit: start-limit-hit not reported"
grep -qF 'recover with: sudo systemctl reset-failed joustmania && sudo systemctl start joustmania' "${CASE}/out.log" \
  || fail "start-limit: no recovery command"

# --- a game image missing (BR-001, BR-002) ----------------------------------
make_case missing
echo i/menu:9.9.9 >"${CASE}/missing"
echo menu >"${CASE}/exited"
expect missing 2 failed
grep -qx 'FAIL image missing: menu (i/menu:9.9.9)' "${CASE}/out.log" || fail "missing: the missing image is not named"
grep -qF "${CASE}/repo/scripts/setup/joustmania-upgrade.sh joustmania-v9.9.9" "${CASE}/out.log" \
  || fail "missing: no exact joustmania-upgrade.sh recovery command"

# --- observability down: degraded (A1) --------------------------------------
make_case obs-down
echo jaeger >"${CASE}/exited"
expect obs-down 1 degraded
grep -qx 'WARN service jaeger: exited' "${CASE}/out.log" || fail "obs-down: jaeger not reported"

# --- a game service running but unhealthy: FAIL with the restart (#2125) ----
make_case unhealthy
echo rust-hid >"${CASE}/unhealthy"
expect unhealthy 2 failed
grep -qx 'FAIL service rust-hid: running (unhealthy); recover with: docker restart joustmania-rust-hid' "${CASE}/out.log" \
  || fail "unhealthy: rust-hid not FAIL with its restart command: $(grep rust-hid "${CASE}/out.log")"

# --- an observability service unhealthy: degraded, with the restart ---------
make_case obs-unhealthy
echo jaeger >"${CASE}/unhealthy"
expect obs-unhealthy 1 degraded
grep -qx 'WARN service jaeger: running (unhealthy); recover with: docker restart joustmania-jaeger' "${CASE}/out.log" \
  || fail "obs-unhealthy: jaeger not WARN with its restart command: $(grep jaeger "${CASE}/out.log")"

# --- clock not synchronised -------------------------------------------------
make_case clock
echo no >"${CASE}/ntp"
expect clock 0 ok
grep -q '^INFO clock: not NTP synchronised' "${CASE}/out.log" || fail "clock: the unsynced clock is not reported as INFO"

# --- docker not reachable: never "images missing" ---------------------------
make_case no-docker
touch "${CASE}/info-fails"
expect no-docker 2 failed
grep -q '^FAIL docker: not reachable' "${CASE}/out.log" || fail "no-docker: no 'docker: not reachable'"
grep -qi 'missing' "${CASE}/out.log" && fail "no-docker: reported as missing images: $(grep -i missing "${CASE}/out.log")"

# --- the unit is still starting ---------------------------------------------
make_case activating
echo activating >"${CASE}/active"
echo menu >"${CASE}/exited"
expect activating 1 starting
grep -qx 'INFO service menu: exited' "${CASE}/out.log" || fail "activating: a game service not yet up is not INFO"

# --- a branch checkout (A4) -------------------------------------------------
make_case branch
git -C "${CASE}/repo" checkout -q -b stage
expect branch 1 degraded
grep -q '^WARN version: not pinned (branch stage' "${CASE}/out.log" || fail "branch: not-pinned not reported"

# --- start.env fallback: no RuntimeDirectory -> /tmp/joustmania -------------
make_case fallback
mkdir -p "${CASE}/tmpstate"
printf 'phase=start\nresult=degraded\nts=x\n' >"${CASE}/tmpstate/start.env"
env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_LED_STATE_DIR="${CASE}/ledstate" JOUSTMANIA_LED_SYSFS="${CASE}/sys" JOUSTMANIA_RUN_STATE_DIR="${CASE}/no-run" JOUSTMANIA_TMP_STATE_DIR="${CASE}/tmpstate" \
  bash "${CASE}/repo/scripts/setup/joustmania-status.sh" >"${CASE}/out.log" 2>&1 || true
grep -qx 'INFO last start: result=degraded ts=x' "${CASE}/out.log" || fail "fallback: /tmp start.env not read: $(grep 'last start' "${CASE}/out.log")"
mkdir -p "${CASE}/run"
printf 'phase=start\nresult=ok\nts=y\n' >"${CASE}/run/start.env"
env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_LED_STATE_DIR="${CASE}/ledstate" JOUSTMANIA_LED_SYSFS="${CASE}/sys" JOUSTMANIA_RUN_STATE_DIR="${CASE}/run" JOUSTMANIA_TMP_STATE_DIR="${CASE}/tmpstate" \
  bash "${CASE}/repo/scripts/setup/joustmania-status.sh" >"${CASE}/out.log" 2>&1 || true
grep -qx 'INFO last start: result=ok ts=y' "${CASE}/out.log" || fail "fallback: the RuntimeDirectory start.env does not win: $(grep 'last start' "${CASE}/out.log")"

# --- the ACT LED line (C-042) -----------------------------------------------
make_case led-line
mkdir -p "${CASE}/sys/ACT"
printf 'none [timer] heartbeat default-on mmc0\n' >"${CASE}/sys/ACT/trigger"
echo failed >"${CASE}/ledstate/state"
printf '#!/bin/bash\n# an older copy\n' >"${CASE}/helper"
expect led-line 0 ok
grep -qx 'INFO led: failed (ACT trigger timer)' "${CASE}/out.log" || fail "led-line: $(grep 'led' "${CASE}/out.log")"
grep -q '^INFO led helper: .* differs from this checkout; update it with: sudo .*install_autostart.sh' "${CASE}/out.log" \
  || fail "led-line: a stale installed helper is not named"
cp "${CASE}/repo/scripts/setup/joustmania-led.sh" "${CASE}/helper"
run_status || true
grep -q '^INFO led helper' "${CASE}/out.log" && fail "led-line: an up-to-date helper is reported"

# --- --led: ExecStartPost, the start has ended (A1, A5) ---------------------
make_case led-ok
echo activating >"${CASE}/active"
expect led-ok 0 ok --led
grep -qx 'OK   unit: start ended (ExecStartPost)' "${CASE}/out.log" || fail "led-ok: activating not taken as started"
[[ "$(cat "${CASE}/run/led.request" 2>/dev/null)" == ok ]] || fail "led-ok: led.request is not ok"

make_case led-degraded
echo activating >"${CASE}/active"
echo jaeger >"${CASE}/exited"
expect led-degraded 1 degraded --led
[[ "$(cat "${CASE}/run/led.request" 2>/dev/null)" == degraded ]] || fail "led-degraded: led.request is not degraded"

make_case led-failed
echo activating >"${CASE}/active"
echo menu >"${CASE}/exited"
printf 'stale\n' >"${CASE}/elsewhere"
ln -s "${CASE}/elsewhere" "${CASE}/run/led.request"
expect led-failed 2 failed --led
[[ ! -L "${CASE}/run/led.request" && "$(cat "${CASE}/run/led.request")" == failed ]] || fail "led-failed: led.request not replaced with failed"
[[ "$(cat "${CASE}/elsewhere")" == stale ]] || fail "led-failed: followed a symlink"

make_case led-usage
run_status --bogus && fail "led-usage: an unknown argument exits 0"

# --- a start refused on a branch checkout (A4): failed, the reason shown -----
make_case branch-refused
git -C "${CASE}/repo" checkout -q -b main
echo failed >"${CASE}/active"
echo exit-code >"${CASE}/result"
printf 'phase=start\nresult=failed\nreason=not pinned (branch main); pin with scripts/setup/joustmania-upgrade.sh\nmissing=\ndown=\nts=2026-10-26T10:00:00+01:00\n' >"${CASE}/status/start.env"
expect branch-refused 2 failed
grep -q '^WARN version: not pinned (branch main' "${CASE}/out.log" || fail "branch-refused: the branch is not named"
grep -q '^INFO last start: result=failed reason=not pinned (branch main); pin with scripts/setup/joustmania-upgrade.sh ts=' "${CASE}/out.log" \
  || fail "branch-refused: the refusal reason is not shown: $(grep 'last start' "${CASE}/out.log")"

# --- git cannot read the checkout: named as such, not as a branch (A4) -------
make_case git-unreadable
printf '#!/usr/bin/env bash\necho "fatal: not a git repository" >&2\nexit 128\n' >"${CASE}/bin/git"
chmod +x "${CASE}/bin/git"
expect git-unreadable 1 degraded
grep -qx "WARN version: git cannot read the checkout (check: git -C ${CASE}/repo status)" "${CASE}/out.log" \
  || fail "git-unreadable: not reported as unreadable: $(grep version "${CASE}/out.log")"
grep -q 'not pinned (branch' "${CASE}/out.log" && fail "git-unreadable: reported as a branch checkout"

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: joustmania-status reports version, unit, images, services and clock"
