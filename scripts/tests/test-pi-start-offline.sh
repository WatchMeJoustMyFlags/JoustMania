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
#  - depends_on: the stub `up` aborts like compose when a named service's
#    dependency chain has a missing image; such services are left out and
#    named, everything else starts (BR-002)
#  - docker not reachable: exit 1 with "docker not reachable", no missing-image
#    report and no `up`
#  - a branch checkout (not pinned): exit 1, state failed with the reason and
#    the joustmania-upgrade.sh command that pins it, no container started or
#    removed (A4)
#  - git cannot read the checkout (git exits 128, no .git, no git binary): no
#    branch is proven, so the game starts, state degraded with the reason (A4)
# Runs as part of `make lint`, or directly: ./scripts/tests/test-pi-start-offline.sh
# UC: UC-060 main step 1 step 2 step 3 step 4 step 5 BR-001 BR-002 BR-004 A1 A4 A5 Pre-2 Post-S-1 Post-F-1
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

# In `compose config` order: sorted by name, like real compose (bytewise)
GAME="audio connect-proxy controller-manager dashboard envoy flag-service flagd game-coordinator menu pairing-daemon rust-hid"
OBS="agent grafana jaeger loki otel-collector victoria-metrics"
# depends_on as in docker-compose.yml (service: deps)
deps_of() {
  case "$1" in
    flagd) echo flag-service ;;
    controller-manager) echo flagd rust-hid ;;
    pairing-daemon) echo rust-hid ;;
    game-coordinator | menu | audio | agent) echo flagd ;;
    grafana) echo jaeger loki victoria-metrics ;;
  esac
}

# make_case <name>: ${WORK}/<name>/{repo,bin,docker.log,git.log,curl.log}; sets CASE
#   $CASE/missing: images `docker image inspect` does not find (one per line)
#   $CASE/down: game services `compose ps` shows as exited
#   $CASE/obs-up-fails: the observability `up --wait` exits 1
#   $CASE/up-hangs: the game `up` never returns
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
    for s in $(printf '%s\n' ${GAME} ${OBS} | LC_ALL=C sort); do
      printf '  %s:\n' "$s"
      d="$(deps_of "$s")"
      if [[ -n "$d" ]]; then
        printf '    depends_on:\n'
        for x in $d; do printf '      %s:\n        condition: service_started\n        required: true\n' "$x"; done
      fi
      printf '    environment:\n      K: v\n    image: ghcr.io/o/joustmania/%s:0.0.0-test\n' "$s"
    done
    printf 'networks:\n  joustmania:\n    name: j\n'
  } >"${CASE}/config"
  # deps: "service dep" lines, for the stub `up` (compose starts dependencies
  # and aborts the whole up on a missing image anywhere in that chain)
  for s in ${GAME} ${OBS}; do for x in $(deps_of "$s"); do echo "$s $x"; done; done >"${CASE}/deps"
  cat >"${CASE}/bin/docker" <<STUB
#!/usr/bin/env bash
C="${CASE}"
echo "\$*" >>"\$C/docker.log"
[[ "\$1" == info ]] && { [[ -e "\$C/info-fails" ]] && exit 1; exit 0; }
if [[ "\$1" == compose && " \$* " == *" up "* ]]; then
  [[ -e "\$C/up-hangs" && " \$* " != *" --wait "* ]] && exec sleep 60
  todo=(); seen=" "
  for a in "\$@"; do [[ "\$a" == -* || "\$a" =~ ^[0-9]+\$ || "\$a" == compose || "\$a" == up || "\$a" == never || "\$a" == *docker-compose.yml || "\$a" == .env* ]] || todo+=("\$a"); done
  while ((\${#todo[@]})); do
    s="\${todo[0]}"; todo=("\${todo[@]:1}")
    [[ "\$seen" == *" \$s "* ]] && continue
    seen+="\$s "
    if grep -qxF "ghcr.io/o/joustmania/\$s:0.0.0-test" "\$C/missing"; then echo "No such image for \$s" >&2; exit 1; fi
    for x in \$(sed -n "s/^\$s //p" "\$C/deps"); do todo+=("\$x"); done
  done
fi
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
  # A pinned checkout (detached HEAD at a commit); a branch is refused (A4)
  "${REAL_GIT}" -C "${CASE}/repo" init -q
  "${REAL_GIT}" -C "${CASE}/repo" -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -q --allow-empty -m pin
  "${REAL_GIT}" -C "${CASE}/repo" checkout -q --detach
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
"${REAL_GIT}" -C "${CASE}/repo" -c user.name=t -c user.email=t@t -c tag.gpgsign=false tag joustmania-v9.9.9
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
grep -qxF "${OBS_UP} agent loki otel-collector victoria-metrics" "${CASE}/docker.log" \
  || fail "obs-missing: the other observability services (not grafana, which needs jaeger) did not start: $(grep ' up ' "${CASE}/docker.log")"
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

# --- 6. a dependency's image is missing: its dependents are left out ---------
make_case dep-obs
echo "ghcr.io/o/joustmania/loki:0.0.0-test" >"${CASE}/missing"
run_start || fail "dep-obs: start failed: $(tail -3 "${CASE}/out.log")"
grep -qxF "${OBS_UP} agent jaeger otel-collector victoria-metrics" "${CASE}/docker.log" \
  || fail "dep-obs: observability phase does not leave out loki and grafana (which needs it): $(grep ' up ' "${CASE}/docker.log")"
grep -q 'need a service whose image is missing: grafana' "${CASE}/out.log" || fail "dep-obs: grafana not named as not started"
[[ "$(state result)" == degraded ]] || fail "dep-obs: state is not degraded: $(state result)"
make_case dep-game
echo "ghcr.io/o/joustmania/flag-service:0.0.0-test" >"${CASE}/missing"
if run_start; then fail "dep-game: start succeeded with the flag-service image missing"; fi
grep -qxF "${GAME_UP} connect-proxy dashboard envoy pairing-daemon rust-hid" "${CASE}/docker.log" \
  || fail "dep-game: the services that do not need flag-service did not start: $(grep ' up ' "${CASE}/docker.log")"
grep -q 'waiting on those: audio controller-manager flagd game-coordinator menu' "${CASE}/out.log" \
  || fail "dep-game: the blocked game services are not named: $(grep FAIL "${CASE}/out.log")"
[[ "$(state down)" == *"flagd"* ]] || fail "dep-game: start.env does not list the blocked services: $(state down)"

# --- 6b. the game `up` hangs: bounded by the game-phase timeout -------------
make_case up-hangs
touch "${CASE}/up-hangs"
echo controller-manager >"${CASE}/down"
start_s="${SECONDS}"
if run_start; then fail "up-hangs: start succeeded although up hung"; fi
((SECONDS - start_s <= 8)) || fail "up-hangs: took $((SECONDS - start_s)) s (the game up is not bounded)"
grep -q 'compose up exited 124' "${CASE}/out.log" || fail "up-hangs: the hung up was not cut off by timeout: $(grep -i 'up exited' "${CASE}/out.log")"
grep -q -- '--wait' "${CASE}/docker.log" && fail "up-hangs: observability was started"

# --- 7. docker not reachable: not reported as missing images -----------------
make_case no-docker
touch "${CASE}/info-fails"
if run_start; then fail "no-docker: start succeeded without docker"; fi
grep -q 'FAIL: docker not reachable' "${CASE}/out.log" || fail "no-docker: no 'docker not reachable': $(tail -2 "${CASE}/out.log")"
grep -qi 'missing\|joustmania-upgrade.sh [^w]' "${CASE}/out.log" && fail "no-docker: reported as missing images: $(grep -i missing "${CASE}/out.log")"
grep -q ' up ' "${CASE}/docker.log" && fail "no-docker: up was called"

# --- 8. a branch checkout: refused, nothing started (A4) ---------------------
make_case branch
"${REAL_GIT}" -C "${CASE}/repo" checkout -q -b main
branch_sha="$("${REAL_GIT}" -C "${CASE}/repo" rev-parse HEAD)"
if run_start; then fail "branch: start succeeded on a branch checkout"; fi
no_network branch
grep -qE ' (up|rm|stop|down)( |$)' "${CASE}/docker.log" && fail "branch: a container was started or removed: $(grep -E ' (up|rm|stop|down)( |$)' "${CASE}/docker.log")"
grep -q 'FAIL: the checkout is not pinned (branch main' "${CASE}/out.log" || fail "branch: no FAIL naming the branch: $(tail -3 "${CASE}/out.log")"
grep -qF "${CASE}/repo/scripts/setup/joustmania-upgrade.sh ${branch_sha}" "${CASE}/out.log" \
  || fail "branch: no exact joustmania-upgrade.sh command to pin it: $(tail -2 "${CASE}/out.log")"
[[ "$(state result)" == failed ]] || fail "branch: state is not failed: $(state result)"
[[ "$(state reason)" == "not pinned (branch main)"* ]] || fail "branch: start.env has no reason: $(state reason)"

# --- 9. git cannot read the checkout: fail open, degraded (A4) ---------------
# git exits 128 for everything (damaged repo, dubious ownership), no .git at
# all, no git binary (127): a branch is not proven, so the game starts.
for variant in git-128 no-dotgit no-git; do
  make_case "${variant}"
  case "${variant}" in
    git-128) printf '#!/usr/bin/env bash\necho "fatal: not a git repository" >&2\nexit 128\n' >"${CASE}/bin/git" ;;
    no-dotgit) rm -rf "${CASE}/repo/.git" ;;
    no-git) printf '#!/usr/bin/env bash\necho "git: command not found" >&2\nexit 127\n' >"${CASE}/bin/git" ;;
  esac
  run_start || fail "${variant}: the start failed although no branch is proven: $(tail -3 "${CASE}/out.log")"
  grep -qxF "${GAME_UP} ${GAME}" "${CASE}/docker.log" || fail "${variant}: the game did not start: $(grep ' up ' "${CASE}/docker.log")"
  grep -q 'WARN: git cannot read the checkout' "${CASE}/out.log" || fail "${variant}: no WARN: $(grep -i git "${CASE}/out.log")"
  [[ "$(state result)" == degraded ]] || fail "${variant}: state is not degraded: $(state result)"
  [[ "$(state reason)" == "git cannot read the checkout" ]] || fail "${variant}: start.env reason: $(state reason)"
done

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: the Pi start pulls nothing, names missing images with the recovery and starts the game first"
