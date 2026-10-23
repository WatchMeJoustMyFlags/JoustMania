#!/usr/bin/env bash
# The Pi start script never tears a running stack down (UC-060 BR-001, NFR-026, #2027 PR 2a).
#
# joustmania-start.sh runs against a stub docker (and a stub git, amixer):
#  - no `rm -f` except a foreign joustmania-* container (or python-hid when its
#    profile is inactive), no `prune`, no `down`; the last call is `up -d --remove-orphans`
#  - "ours" is what `compose ps -a` lists (so a COMPOSE_PROJECT_NAME in .env.local
#    counts), and when compose cannot list them nothing is removed
#  - the effective IMAGE_TAG comes from `compose config --images`, not from a grep
#    of .env (the release-please inline comment stays out of the log and start.env)
#  - a long `config --services` list does not make the python-hid check fail
#    under pipefail (audit F4); an active python-hid is not removed
#  - an amixer that fails does not stop the start (errexit + pipefail)
#  - state goes to $RUNTIME_DIRECTORY/start.env and an unwritable state dir is not fatal
#  - without JOUSTMANIA_UNIT_REV (outdated unit) every start logs "unit outdated" and a failed
#    start exits 0; with it a failed start exits non-zero
# Runs as part of `make lint`, or directly: ./scripts/tests/test-pi-start-safe.sh
# UC: UC-060 A5 Pre-1 Post-F-1
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
  cp "${ROOT}/scripts/setup/joustmania-start.sh" "${ROOT}/scripts/setup/joustmania-compose.sh" "${CASE}/repo/scripts/setup/"
  printf 'IMAGE_TAG=0.0.0-test\n' >"${CASE}/repo/.env"
  : >"${CASE}/docker.log"
  # docker stub. ps -a --format prints $CASE/containers (name|project|working_dir);
  # compose ps -a prints $CASE/own (fails when $CASE/ps-fails exists);
  # compose config --services / --images print $CASE/services / $CASE/images (exec:
  # like the real compose, the writer dies of SIGPIPE when the reader exits early);
  # "up" fails when $CASE/up-fails exists.
  cat >"${CASE}/bin/docker" <<STUB
#!/usr/bin/env bash
echo "\$*" >>"${CASE}/docker.log"
if [[ "\$1" == ps ]]; then cat "${CASE}/containers" 2>/dev/null; exit 0; fi
if [[ "\$1" == compose && " \$* " == *" ps "* ]]; then
  [[ -e "${CASE}/ps-fails" ]] && exit 1
  cat "${CASE}/own" 2>/dev/null; exit 0
fi
if [[ "\$1" == compose && " \$* " == *" config --services "* ]]; then exec cat "${CASE}/services"; fi
if [[ "\$1" == compose && " \$* " == *" config --images "* ]]; then exec cat "${CASE}/images"; fi
if [[ "\$1" == compose && " \$* " == *" up "* && -e "${CASE}/up-fails" ]]; then exit 1; fi
exit 0
STUB
  printf '#!/usr/bin/env bash\nexit 1\n' >"${CASE}/bin/git" # offline: pull fails, non-fatal
  printf '#!/usr/bin/env bash\nexit 0\n' >"${CASE}/bin/amixer"
  chmod +x "${CASE}/bin/"*
  printf 'rust-hid\nflagd\n' >"${CASE}/services"
  printf 'ghcr.io/o/joustmania/flag-service:0.0.0-test\nghcr.io/o/joustmania/game-coordinator-service:0.0.0-test\n' >"${CASE}/images"
}

# run_start <extra env...>; returns the script's exit code
run_start() {
  env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_DOCKER_WAIT_S=1 JOUSTMANIA_ASOUND_DIR="${CASE}/asound" \
    JOUSTMANIA_STATUS_DIR="${CASE}/status" "$@" \
    bash "${CASE}/repo/scripts/setup/joustmania-start.sh" >"${CASE}/out.log" 2>&1
}

# --- 1. no teardown, foreign container removed, own kept --------------------
make_case teardown
printf 'joustmania-menu|repo|/elsewhere\njoustmania-old||\njoustmania-other|someproject|/other\njoustmania-audio|repo|%s\n' \
  "${CASE}/repo" >"${CASE}/containers"
printf 'joustmania-menu\n' >"${CASE}/own"
run_start || fail "teardown: start exited non-zero"
log="${CASE}/docker.log"
if grep -qE '(^| )(prune|down)( |$)' "${log}"; then fail "teardown: prune or down was called"; fi
grep -qE 'joustmania-(menu|audio)' <(grep -E '^(rm|container rm)' "${log}" || true) && fail "teardown: our own container was removed"
grep -qx 'rm -f joustmania-old' "${log}" || fail "teardown: foreign container (no project label) not removed"
grep -qx 'rm -f joustmania-other' "${log}" || fail "teardown: foreign container (other project) not removed"
rm_calls="$(grep -E '^rm ' "${log}" | grep -vxE 'rm -f (joustmania-old|joustmania-other|joustmania-python-hid)' || true)"
[[ -z "${rm_calls}" ]] || fail "teardown: unexpected rm call(s): ${rm_calls}"
[[ "$(tail -1 "${log}")" == "compose -f docker-compose.yml --env-file .env up -d --remove-orphans" ]] \
  || fail "teardown: last docker call is not 'up -d --remove-orphans': $(tail -1 "${log}")"
grep -q 'IMAGE_TAG 0.0.0-test' "${CASE}/out.log" || fail "teardown: the effective IMAGE_TAG is not logged"
if grep -q '^jq\|jq ' "${ROOT}/scripts/setup/joustmania-start.sh"; then fail "teardown: the start script still needs jq"; fi
grep -q 'unit outdated or not run by the rev-2 unit' "${CASE}/out.log" \
  || fail "teardown: a successful start without JOUSTMANIA_UNIT_REV does not say the unit is outdated"

# --- 1b. COMPOSE_PROJECT_NAME in .env.local: compose's own containers stay ---
make_case project-name
printf 'COMPOSE_PROJECT_NAME=stage\n' >"${CASE}/repo/.env.local"
printf 'joustmania-menu|stage|/elsewhere\njoustmania-game-coordinator|stage|/elsewhere\n' >"${CASE}/containers"
printf 'joustmania-menu\njoustmania-game-coordinator\n' >"${CASE}/own"
run_start || fail "project-name: start exited non-zero"
if grep -qE '^rm -f joustmania-(menu|game-coordinator)$' "${CASE}/docker.log"; then
  fail "project-name: a container of the .env.local project was removed as foreign"
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
  RUNTIME_DIRECTORY="${CASE}/run" bash "${CASE}/repo/scripts/setup/joustmania-start.sh" >"${CASE}/out.log" 2>&1 \
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

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: the Pi start script tears nothing down, survives pipefail and an outdated unit"
