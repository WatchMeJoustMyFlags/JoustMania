#!/usr/bin/env bash
# The Pi start is offline-safe and starts the game before observability
# (UC-060 BR-001, BR-002, BR-004, A1, A5; FR-180, FR-181; #2027 PR 2b).
#
# joustmania-start.sh runs against a stub docker, a logging git (the real one
# underneath) and a curl that records any call:
#  - no `git pull`/`fetch`, no `compose pull`, no curl: every `up` is `--pull never`
#  - game first: `up -d --pull never --remove-orphans <game services>`, judged
#    from `compose ps`; then `up -d --pull never --wait --wait-timeout N <observability + agent>`
#  - a missing game image: the others still start, the start fails at once with
#    the missing image and the joustmania-upgrade.sh recovery command (the
#    pinned tag when HEAD has one), observability is not started
#  - a missing observability image, or an observability phase that fails:
#    exit 0, state degraded, the game is not touched
#  - a game service that never runs: exit 1, observability not started
# Runs as part of `make lint`, or directly: ./scripts/tests/test-pi-start-offline.sh
# UC: UC-060 BR-001 BR-002 BR-004 A1 A5
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unset COMPOSE DOCKER COMPOSE_PROJECT_NAME COMPOSE_PROFILES JOUSTMANIA_UNIT_REV RUNTIME_DIRECTORY IMAGE_TAG || true
# A test that reaches for the real docker fails instead of using it
export PATH="${ROOT}/scripts/tests/lib/fail-docker:${PATH}"
REAL_GIT="$(command -v git)"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

GAME="flag-service flagd rust-hid controller-manager pairing-daemon game-coordinator menu audio connect-proxy dashboard envoy"
OBS="agent grafana jaeger loki otel-collector victoria-metrics"

# make_case <name>: ${WORK}/<name>/{repo,bin,docker.log,git.log,curl.log}; sets CASE
#   $CASE/missing: images `docker image inspect` does not find (one per line)
#   $CASE/down: game services `compose ps` shows as exited
#   $CASE/obs-up-fails: the observability `up --wait` exits 1
make_case() {
  CASE="${WORK}/$1"
  mkdir -p "${CASE}/repo/scripts/setup" "${CASE}/bin"
  cp "${ROOT}/scripts/setup/joustmania-start.sh" "${ROOT}/scripts/setup/joustmania-compose.sh" \
    "${ROOT}/scripts/setup/lib-joustmania.sh" "${CASE}/repo/scripts/setup/"
  printf 'IMAGE_TAG=0.0.0-test\n' >"${CASE}/repo/.env"
  : >"${CASE}/docker.log"
  : >"${CASE}/git.log"
  : >"${CASE}/curl.log"
  : >"${CASE}/missing"
  : >"${CASE}/down"
  {
    printf 'name: repo\nservices:\n'
    for s in ${GAME} ${OBS}; do printf '  %s:\n    environment:\n      K: v\n    image: ghcr.io/o/joustmania/%s:0.0.0-test\n' "$s" "$s"; done
    printf 'networks:\n  joustmania:\n    name: j\n'
  } >"${CASE}/config"
  cat >"${CASE}/bin/docker" <<STUB
#!/usr/bin/env bash
C="${CASE}"
echo "\$*" >>"\$C/docker.log"
case " \$* " in
  *" config --services "*) sed -n 's/^  \([a-z-]*\):\$/\1/p' "\$C/config"; exit 0 ;;
  *" config --images "*) sed -n 's/^    image: //p' "\$C/config"; exit 0 ;;
  *" compose "*" config "*) cat "\$C/config"; exit 0 ;;
  *" compose "*" ps "*"{{.Service}}"*)
    for s in ${GAME}; do
      if grep -qxF "\$s" "\$C/down"; then echo "\$s|exited|"; else echo "\$s|running|"; fi
    done; exit 0 ;;
  *" compose "*" ps "*) exit 0 ;;
  *" compose "*" up "*"--wait"*) [[ -e "\$C/obs-up-fails" ]] && exit 1; exit 0 ;;
  " image inspect "*) grep -qxF "\$3" "\$C/missing" && exit 1; exit 0 ;;
esac
exit 0
STUB
  printf '#!/usr/bin/env bash\necho "$*" >>"%s/git.log"\nexec "%s" "$@"\n' "${CASE}" "${REAL_GIT}" >"${CASE}/bin/git"
  printf '#!/usr/bin/env bash\necho "$*" >>"%s/curl.log"\nexit 7\n' "${CASE}" >"${CASE}/bin/curl"
  printf '#!/usr/bin/env bash\nexit 0\n' >"${CASE}/bin/amixer"
  chmod +x "${CASE}/bin/"*
}

# run_start: as the rev-2 unit runs it; returns the script's exit code
run_start() {
  env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_UNIT_REV=2 JOUSTMANIA_DOCKER_WAIT_S=1 \
    JOUSTMANIA_ASOUND_DIR="${CASE}/none" JOUSTMANIA_GAME_READY_S=1 JOUSTMANIA_POLL_S=0.2 \
    JOUSTMANIA_STATUS_DIR="${CASE}/status" \
    bash "${CASE}/repo/scripts/setup/joustmania-start.sh" >"${CASE}/out.log" 2>&1
}

PREFIX="compose -f docker-compose.yml --env-file .env"
GAME_UP="${PREFIX} up -d --pull never --remove-orphans"
OBS_UP="${PREFIX} up -d --pull never --wait --wait-timeout 300"

# no_network <label>: nothing reached for the network or changed the checkout
no_network() {
  grep -qE '(^| )pull( |$)' <(grep -v -- '--pull never' "${CASE}/docker.log") && fail "$1: compose pull was called"
  grep -qE '(^| )(pull|fetch|checkout|reset|merge)( |$)' "${CASE}/git.log" && fail "$1: git changed or fetched the checkout: $(cat "${CASE}/git.log")"
  [[ -s "${CASE}/curl.log" ]] && fail "$1: curl was called (registry contact at boot)"
  grep -E ' up ' "${CASE}/docker.log" | grep -v -- '--pull never' | grep -q . && fail "$1: an up without --pull never"
  return 0
}
state() { sed -n "s/^$1=//p" "${CASE}/status/start.env" 2>/dev/null; }

# --- 1. offline, all images present: no pull, game then observability --------
make_case offline
run_start || fail "offline: start failed: $(tail -3 "${CASE}/out.log")"
no_network offline
grep -qxF "${GAME_UP} ${GAME}" "${CASE}/docker.log" || fail "offline: game phase is not exactly the game services: $(grep ' up ' "${CASE}/docker.log")"
grep -qxF "${OBS_UP} ${OBS}" "${CASE}/docker.log" || fail "offline: observability phase is not exactly the observability services: $(grep ' up ' "${CASE}/docker.log")"
game_line="$(grep -nxF "${GAME_UP} ${GAME}" "${CASE}/docker.log" | cut -d: -f1 | head -n 1)"
obs_line="$(grep -nxF "${OBS_UP} ${OBS}" "${CASE}/docker.log" | cut -d: -f1 | head -n 1)"
judge_line="$(grep -n '{{.Service}}' "${CASE}/docker.log" | cut -d: -f1 | head -n 1)"
[[ -n "${game_line}" && -n "${obs_line}" && -n "${judge_line}" && "${game_line}" -lt "${judge_line}" && "${judge_line}" -lt "${obs_line}" ]] \
  || fail "offline: order is not game up -> game judged -> observability up (lines ${game_line:-?} ${judge_line:-?} ${obs_line:-?})"
[[ "$(state result)" == ok ]] || fail "offline: state is not ok: $(state result)"
grep -q 'K: v' "${CASE}/out.log" && fail "offline: the full compose config was printed"

# --- 2. a game image is missing: fail at once, with the image and the recovery
make_case game-missing
echo "ghcr.io/o/joustmania/menu:0.0.0-test" >"${CASE}/missing"
"${REAL_GIT}" -C "${CASE}/repo" init -q
"${REAL_GIT}" -C "${CASE}/repo" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q --allow-empty -m pin
"${REAL_GIT}" -C "${CASE}/repo" -c user.name=t -c user.email=t@t -c tag.gpgsign=false tag joustmania-v9.9.9
"${REAL_GIT}" -C "${CASE}/repo" checkout -q --detach
: >"${CASE}/git.log"
start_s="${SECONDS}"
if run_start; then fail "game-missing: start succeeded with the menu image missing"; fi
((SECONDS - start_s <= 5)) || fail "game-missing: took $((SECONDS - start_s)) s (must fail at once, no readiness wait)"
no_network game-missing
grep -q 'menu(ghcr.io/o/joustmania/menu:0.0.0-test)' "${CASE}/out.log" || fail "game-missing: the missing image is not named: $(tail -2 "${CASE}/out.log")"
grep -qF "${CASE}/repo/scripts/setup/joustmania-upgrade.sh joustmania-v9.9.9" "${CASE}/out.log" \
  || fail "game-missing: no exact joustmania-upgrade.sh recovery command: $(grep -i recover "${CASE}/out.log")"
grep -qxF "${GAME_UP} ${GAME/ menu / }" "${CASE}/docker.log" || fail "game-missing: the services with an image did not start (BR-002): $(grep ' up ' "${CASE}/docker.log")"
grep -q -- '--wait' "${CASE}/docker.log" && fail "game-missing: observability was started"
grep -qE '(^| )(stop|down|rm)( |$)' <(grep '^compose' "${CASE}/docker.log") && fail "game-missing: something was torn down"
[[ "$(state result)" == failed ]] || fail "game-missing: state is not failed: $(state result)"
[[ "$(state missing)" == *"menu("* ]] || fail "game-missing: start.env does not list the missing image: $(state missing)"

# --- 3. an observability image is missing: degraded, the rest starts ---------
make_case obs-missing
echo "ghcr.io/o/joustmania/jaeger:0.0.0-test" >"${CASE}/missing"
run_start || fail "obs-missing: start failed: $(tail -3 "${CASE}/out.log")"
no_network obs-missing
grep -qxF "${OBS_UP} ${OBS/ jaeger / }" "${CASE}/docker.log" || fail "obs-missing: the other observability services did not start: $(grep ' up ' "${CASE}/docker.log")"
grep -q 'jaeger(ghcr.io/o/joustmania/jaeger:0.0.0-test)' "${CASE}/out.log" || fail "obs-missing: the missing image is not named"
grep -q 'joustmania-upgrade.sh' "${CASE}/out.log" || fail "obs-missing: no recovery command"
[[ "$(state result)" == degraded ]] || fail "obs-missing: state is not degraded: $(state result)"

# --- 4. observability fails: degraded, exit 0, the game is left alone -------
make_case obs-fails
touch "${CASE}/obs-up-fails"
run_start || fail "obs-fails: a failed observability phase failed the unit"
[[ "$(state result)" == degraded ]] || fail "obs-fails: state is not degraded: $(state result)"
grep -qE '(^| )(stop|down|rm)( |$)' <(grep '^compose' "${CASE}/docker.log") && fail "obs-fails: something was torn down"

# --- 5. a game service never runs: failed, observability not started ---------
make_case game-down
echo controller-manager >"${CASE}/down"
if run_start; then fail "game-down: start succeeded with controller-manager exited"; fi
grep -q 'controller-manager(exited)' "${CASE}/out.log" || fail "game-down: the service that is down is not named"
grep -q -- '--wait' "${CASE}/docker.log" && fail "game-down: observability was started"
[[ "$(state down)" == *"controller-manager(exited)"* ]] || fail "game-down: start.env does not list it: $(state down)"

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: the Pi start pulls nothing, names missing images with the recovery and starts the game first"
