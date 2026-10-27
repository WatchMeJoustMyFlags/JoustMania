#!/usr/bin/env bash
# The Pi start script never tears a running stack down (UC-060 BR-001, NFR-026, #2027 PR 2a).
#
# joustmania-start.sh runs against a stub docker (and a stub git, amixer):
#  - no `rm -f` except a foreign joustmania-* container (or python-hid / operator-pad when its
#    profile is inactive), no `prune`, no `down`; the game starts with
#    `up -d --pull never --remove-orphans <services>`
#  - "ours" is what `compose ps -a` lists (so a COMPOSE_PROJECT_NAME in .env.local
#    counts), and when compose cannot list them nothing is removed; a container from
#    this checkout under another project name is foreign (it would block `up -d`)
#  - the effective IMAGE_TAG comes from `compose config --images`, not from a grep
#    of .env (the release-please inline comment stays out of the log and start.env)
#  - a long `config --services` list does not make the python-hid check fail
#    under pipefail (audit F4); an active python-hid is not removed
#  - an amixer that fails does not stop the start (errexit + pipefail)
#  - state goes to $RUNTIME_DIRECTORY/start.env and an unwritable state dir is not fatal
#  - without JOUSTMANIA_UNIT_REV (outdated unit) every start logs "unit outdated" and a failed
#    start exits 0; with it a failed start exits non-zero
# Runs as part of `make lint`, or directly: ./scripts/tests/test-pi-start-safe.sh
# UC: UC-060 main step 2 A5 Pre-1 Post-F-1
# NFR: NFR-026
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unset COMPOSE DOCKER COMPOSE_PROJECT_NAME COMPOSE_PROFILES JOUSTMANIA_UNIT_REV RUNTIME_DIRECTORY || true
# A test that reaches for the real docker fails instead of using it
export PATH="${ROOT}/scripts/tests/lib/fail-docker:${PATH}"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

# make_case <name> -> ${WORK}/<name>/{repo,bin,docker.log}; sets CASE
make_case() {
  CASE="${WORK}/$1"
  mkdir -p "${CASE}/repo/scripts/setup" "${CASE}/bin" "${CASE}/asound/card0"
  cp "${ROOT}/scripts/setup/joustmania-start.sh" "${ROOT}/scripts/setup/joustmania-compose.sh" "${ROOT}/scripts/setup/lib-joustmania.sh" "${CASE}/repo/scripts/setup/"
  printf 'IMAGE_TAG=0.0.0-test\n' >"${CASE}/repo/.env"
  : >"${CASE}/docker.log"
  # docker stub. ps -a --format prints $CASE/containers (name|project|working_dir);
  # compose ps -a prints $CASE/own (name|project; fails when $CASE/ps-fails exists);
  # compose config --services / --images print $CASE/services / $CASE/images (exec:
  # like the real compose, the writer dies of SIGPIPE when the reader exits early);
  # bare compose config prints $CASE/config (service -> image YAML); compose ps
  # --format {{.Service}}... prints every configured service running, nothing
  # when "up" failed; "up" fails when $CASE/up-fails exists.
  cat >"${CASE}/bin/docker" <<STUB
#!/usr/bin/env bash
echo "\$*" >>"${CASE}/docker.log"
if [[ "\$1" == ps ]]; then cat "${CASE}/containers" 2>/dev/null; exit 0; fi
if [[ "\$1" == compose && " \$* " == *" ps "*"{{.Service}}"* ]]; then
  [[ -e "${CASE}/up-fails" ]] && exit 0
  sed -n 's/^  \([a-z-]*\):\$/\1|running|/p' "${CASE}/config"; exit 0
fi
if [[ "\$1" == compose && " \$* " == *" ps "* ]]; then
  [[ -e "${CASE}/ps-fails" ]] && exit 1
  cat "${CASE}/own" 2>/dev/null; exit 0
fi
if [[ "\$1" == compose && " \$* " == *" config --services "* ]]; then exec cat "${CASE}/services"; fi
if [[ "\$1" == compose && " \$* " == *" config --images "* ]]; then exec cat "${CASE}/images"; fi
if [[ "\$1" == compose && " \$* " == *" config "* ]]; then exec cat "${CASE}/config"; fi
if [[ "\$1" == compose && " \$* " == *" up "* && -e "${CASE}/up-fails" ]]; then exit 1; fi
exit 0
STUB
  printf '#!/usr/bin/env bash\ncase " $* " in *" symbolic-ref "*) exit 1 ;; *" rev-parse "*) echo 0123456789abcdef0123456789abcdef01234567 ;; *) exit 1 ;; esac\n' >"${CASE}/bin/git" # detached HEAD at a sha: pinned (UC-060 A4)
  printf '#!/usr/bin/env bash\nexit 0\n' >"${CASE}/bin/amixer"
  chmod +x "${CASE}/bin/"*
  printf 'rust-hid\nflagd\n' >"${CASE}/services"
  printf 'ghcr.io/o/joustmania/flag-service:0.0.0-test\nghcr.io/o/joustmania/game-coordinator-service:0.0.0-test\n' >"${CASE}/images"
  printf 'name: repo\nservices:\n  flagd:\n    image: f:1\n  game-coordinator:\n    image: g:1\n  rust-hid:\n    image: r:1\nvolumes:\n  x:\n    name: y\n' >"${CASE}/config"
}

# run_start <extra env...>; returns the script's exit code
run_start() {
  env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_DOCKER_WAIT_S=1 JOUSTMANIA_ASOUND_DIR="${CASE}/asound" \
    JOUSTMANIA_GAME_READY_S=1 JOUSTMANIA_POLL_S=0.2 JOUSTMANIA_STATUS_DIR="${CASE}/status" "$@" \
    bash "${CASE}/repo/scripts/setup/joustmania-start.sh" >"${CASE}/out.log" 2>&1
}

# --- 1. no teardown, foreign container removed, own kept --------------------
make_case teardown
printf 'joustmania-menu|repo|/elsewhere\njoustmania-old||\njoustmania-other|someproject|/other\njoustmania-audio|repo|%s\n' \
  "${CASE}/repo" >"${CASE}/containers"
printf 'joustmania-menu|repo\n' >"${CASE}/own"
run_start || fail "teardown: start exited non-zero"
log="${CASE}/docker.log"
if grep -qE '(^| )(prune|down)( |$)' "${log}"; then fail "teardown: prune or down was called"; fi
grep -qE 'joustmania-(menu|audio)' <(grep -E '^(rm|container rm)' "${log}" || true) && fail "teardown: our own container was removed"
grep -qx 'rm -f joustmania-old' "${log}" || fail "teardown: foreign container (no project label) not removed"
grep -qx 'rm -f joustmania-other' "${log}" || fail "teardown: foreign container (other project) not removed"
rm_calls="$(grep -E '^rm ' "${log}" | grep -vxE 'rm -f (joustmania-old|joustmania-other|joustmania-python-hid|joustmania-operator-pad)' || true)"
[[ -z "${rm_calls}" ]] || fail "teardown: unexpected rm call(s): ${rm_calls}"
grep -qx "compose -f docker-compose.yml --env-file .env up -d --pull never --remove-orphans flagd game-coordinator rust-hid" "${log}" \
  || fail "teardown: no 'up -d --pull never --remove-orphans <game services>' call: $(grep ' up ' "${log}")"
grep -q 'IMAGE_TAG 0.0.0-test' "${CASE}/out.log" || fail "teardown: the effective IMAGE_TAG is not logged"
if grep -q '^jq\|jq ' "${ROOT}/scripts/setup/joustmania-start.sh"; then fail "teardown: the start script still needs jq"; fi
grep -q 'unit outdated or not run by the rev-2 unit' "${CASE}/out.log" \
  || fail "teardown: a successful start without JOUSTMANIA_UNIT_REV does not say the unit is outdated"

# --- 1b. COMPOSE_PROJECT_NAME in .env.local: compose's own containers stay ---
make_case project-name
printf 'COMPOSE_PROJECT_NAME=stage\n' >"${CASE}/repo/.env.local"
printf 'joustmania-menu|stage|/elsewhere\njoustmania-game-coordinator|stage|/elsewhere\n' >"${CASE}/containers"
printf 'joustmania-menu|stage\njoustmania-game-coordinator|stage\n' >"${CASE}/own"
run_start || fail "project-name: start exited non-zero"
if grep -qE '^rm -f joustmania-(menu|game-coordinator)$' "${CASE}/docker.log"; then
  fail "project-name: a container of the .env.local project was removed as foreign"
fi

# --- 1d. same checkout, another project: foreign (else `up -d` hits a name conflict)
make_case stale-project
printf 'COMPOSE_PROJECT_NAME=stage\n' >"${CASE}/repo/.env.local"
printf 'joustmania-menu|stage|%s\njoustmania-audio|stage|%s\njoustmania-agent|joustmania|%s\n' \
  "${CASE}/repo" "${CASE}/repo" "${CASE}/repo" >"${CASE}/containers"
printf 'joustmania-menu|stage\n' >"${CASE}/own"
run_start || fail "stale-project: start exited non-zero"
grep -qx 'rm -f joustmania-agent' "${CASE}/docker.log" \
  || fail "stale-project: a container from this checkout under another project was kept"
if grep -qE '^rm -f joustmania-(menu|audio)$' "${CASE}/docker.log"; then
  fail "stale-project: a container of the current project was removed"
fi

# --- 1c. compose cannot list its containers: nothing is removed ---------------
make_case ps-fails
touch "${CASE}/ps-fails"
printf 'joustmania-menu|repo|/elsewhere\njoustmania-old||\n' >"${CASE}/containers"
run_start || fail "ps-fails: start exited non-zero"
if grep -qE '^rm -f joustmania-(menu|old)$' "${CASE}/docker.log"; then
  fail "ps-fails: a container was removed although compose could not list its own"
fi

# --- 2. F4: long service list, active python-hid stays --------------------
make_case f4
{ echo python-hid; seq 1 20000 | sed 's/^/svc-/'; } >"${CASE}/services"
run_start COMPOSE_PROFILES=python-hid || fail "f4: start exited non-zero"
if grep -qx 'rm -f joustmania-python-hid' "${CASE}/docker.log"; then
  fail "f4: an active python-hid was removed (pipefail + grep -q SIGPIPE)"
fi

# --- 3. a failing amixer does not stop the start -----------------------------
make_case amixer
cat >"${CASE}/bin/amixer" <<'STUB'
#!/usr/bin/env bash
[[ " $* " == *" scontrols "* ]] && { echo "Simple mixer control 'PCM',0"; exit 0; }
exit 1
STUB
run_start || fail "amixer: a failing amixer stopped the start"
grep -q 'Audio volume configured' "${CASE}/out.log" || fail "amixer: start did not get past the audio block"

# --- 4. state file: RUNTIME_DIRECTORY, and an unwritable dir is not fatal -----
make_case state
mkdir "${CASE}/run"
env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_DOCKER_WAIT_S=1 JOUSTMANIA_ASOUND_DIR="${CASE}/asound" \
  JOUSTMANIA_GAME_READY_S=1 RUNTIME_DIRECTORY="${CASE}/run" bash "${CASE}/repo/scripts/setup/joustmania-start.sh" >"${CASE}/out.log" 2>&1 \
  || fail "state: start exited non-zero"
grep -q '^result=ok$' "${CASE}/run/start.env" 2>/dev/null || fail "state: ${CASE}/run/start.env lacks result=ok"
grep -q '^image_tag=0.0.0-test$' "${CASE}/run/start.env" 2>/dev/null || fail "state: start.env lacks the image tag"
make_case image-tag
printf 'IMAGE_TAG=0.7.0 # x-release-please-version\n' >"${CASE}/repo/.env"
printf 'ghcr.io/o/joustmania/game-coordinator-service:0.7.0\n' >"${CASE}/images"
run_start || fail "image-tag: start exited non-zero"
grep -q '^image_tag=0.7.0$' "${CASE}/status/start.env" 2>/dev/null \
  || fail "image-tag: start.env image_tag is not the compose-resolved tag: $(grep image_tag "${CASE}/status/start.env" 2>/dev/null)"
if grep -q 'release-please' "${CASE}/out.log"; then fail "image-tag: the .env inline comment is logged as the tag"; fi
make_case state-ro
run_start JOUSTMANIA_STATUS_DIR=/proc/nonexistent/x || fail "state-ro: an unwritable state dir was fatal"

# --- 5. outdated unit never fails; a current unit does -----------------------
make_case outdated
touch "${CASE}/up-fails"
run_start || fail "outdated: a failed start under a unit without JOUSTMANIA_UNIT_REV exited non-zero"
grep -q 'unit outdated: sudo' "${CASE}/out.log" || fail "outdated: no 'unit outdated' line logged"
make_case current
touch "${CASE}/up-fails"
if run_start JOUSTMANIA_UNIT_REV=2; then fail "current: a failed start exited 0 under a current unit"; fi
if grep -q 'unit outdated' "${CASE}/out.log"; then fail "current: 'unit outdated' logged under rev 2"; fi

# --- 6. NFR-026: a second start with unchanged images recreates nothing ------
# Compose recreates only what `up` is told to or what changed; the script must
# never force it, and never remove or stop what runs. Two starts, same images.
make_case no-recreate
printf 'joustmania-flagd|repo|%s\njoustmania-game-coordinator|repo|%s\n' "${CASE}/repo" "${CASE}/repo" >"${CASE}/containers"
printf 'joustmania-flagd|repo\njoustmania-game-coordinator|repo\n' >"${CASE}/own"
run_start JOUSTMANIA_UNIT_REV=2 || fail "no-recreate: first start exited non-zero"
first_ups="$(grep ' up ' "${CASE}/docker.log")"
: >"${CASE}/docker.log"
run_start JOUSTMANIA_UNIT_REV=2 || fail "no-recreate: second start exited non-zero"
[[ -n "${first_ups}" && "$(grep ' up ' "${CASE}/docker.log")" == "${first_ups}" ]] \
  || fail "no-recreate: the second start ran other up commands than the first"
grep ' up ' "${CASE}/docker.log" | grep -q -- '--pull never' || fail "no-recreate: an up without --pull never"
grep ' up ' "${CASE}/docker.log" | grep -qE -- '--force-recreate|--renew-anon-volumes|--build' && fail "no-recreate: up forces a recreate"
# (the name-based cleanup of an inactive opt-in profile's container is not a recreate)
torn="$(grep -E '(^| )(rm|down|prune|stop|restart|kill)( |$)' "${CASE}/docker.log" | grep -vxE 'rm -f joustmania-(python-hid|operator-pad)' || true)"
[[ -z "${torn}" ]] || fail "no-recreate: something was removed or stopped: ${torn}"

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: the Pi start script tears nothing down, survives pipefail and an outdated unit"
