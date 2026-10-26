#!/usr/bin/env bash
# joustmania-status reports the Pi's state (UC-060 BR-005, A4, A5, BR-006,
# FR-183; #2027 PR 2b). Runs the command against a stub docker, systemctl and
# timedatectl, in a real git checkout pinned at joustmania-v9.9.9:
#  - healthy: every check OK, `state: ok`, exit 0, the pinned version shown
#  - start-limit-hit: FAIL with the recovery command, exit 2
#  - a game image missing: FAIL naming it, the exact joustmania-upgrade.sh
#    command, exit 2
#  - an observability service exited, or the clock not NTP synchronised: WARN,
#    `state: degraded`, exit 1
#  - a branch checkout: WARN not pinned (A4)
#  - no env value is printed
# Runs as part of `make lint`, or directly: ./scripts/tests/test-joustmania-status.sh
# UC: UC-060 BR-005 A4 A5 BR-006
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unset COMPOSE_PROJECT_NAME COMPOSE_PROFILES IMAGE_TAG RUNTIME_DIRECTORY || true
# A test that reaches for the real docker fails instead of using it
export PATH="${ROOT}/scripts/tests/lib/fail-docker:${PATH}"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
SERVICES="flagd game-coordinator menu jaeger"
SECRET="dummy-secret-status-2027"

# make_case <name>: ${WORK}/<name>/{repo,bin}; sets CASE
#   $CASE/missing: images not on the Pi; $CASE/exited: services not running
#   $CASE/active, $CASE/result: the unit's ActiveState / Result; $CASE/ntp
make_case() {
  CASE="${WORK}/$1"
  mkdir -p "${CASE}/repo/scripts/setup" "${CASE}/bin" "${CASE}/status"
  cp "${ROOT}/scripts/setup/joustmania-status.sh" "${ROOT}/scripts/setup/joustmania-compose.sh" \
    "${ROOT}/scripts/setup/lib-joustmania.sh" "${CASE}/repo/scripts/setup/"
  printf 'IMAGE_TAG=0.0.0-test\n' >"${CASE}/repo/.env"
  printf 'JEV_API_KEY=%s\n' "${SECRET}" >"${CASE}/repo/.env.local"
  git -C "${CASE}/repo" init -q
  git -C "${CASE}/repo" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q --allow-empty -m pin
  git -C "${CASE}/repo" -c user.name=t -c user.email=t@t -c tag.gpgsign=false tag joustmania-v9.9.9
  git -C "${CASE}/repo" checkout -q --detach
  : >"${CASE}/missing"
  : >"${CASE}/exited"
  echo active >"${CASE}/active"
  echo success >"${CASE}/result"
  echo yes >"${CASE}/ntp"
  printf 'phase=start\nresult=ok\nmissing=\ndown=\nts=2026-10-26T10:00:00+01:00\n' >"${CASE}/status/start.env"
  cat >"${CASE}/bin/docker" <<STUB
#!/usr/bin/env bash
C="${CASE}"
case " \$* " in
  *" compose "*" config "*)
    printf 'services:\n'
    for s in ${SERVICES}; do printf '  %s:\n    environment:\n      JEV_API_KEY: ${SECRET}\n    image: i/%s:9.9.9\n' "\$s" "\$s"; done
    exit 0 ;;
  *" compose "*" ps "*)
    for s in ${SERVICES}; do
      if grep -qxF "\$s" "\$C/exited"; then echo "\$s|exited|"; else echo "\$s|running|healthy"; fi
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
    bash "${CASE}/repo/scripts/setup/joustmania-status.sh" >"${CASE}/out.log" 2>&1
}
expect() { # $1 = case, $2 = expected exit, $3 = expected state
  local rc=0
  run_status || rc=$?
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

# --- clock not synchronised -------------------------------------------------
make_case clock
echo no >"${CASE}/ntp"
expect clock 1 degraded
grep -q '^WARN clock: not NTP synchronised' "${CASE}/out.log" || fail "clock: no clock warning"

# --- a branch checkout (A4) -------------------------------------------------
make_case branch
git -C "${CASE}/repo" checkout -q -b stage
expect branch 1 degraded
grep -q '^WARN version: not pinned (branch stage' "${CASE}/out.log" || fail "branch: not-pinned not reported"

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: joustmania-status reports version, unit, images, services and clock"
