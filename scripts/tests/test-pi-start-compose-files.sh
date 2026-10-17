#!/usr/bin/env bash
# The Pi start path never loads docker-compose.override.yml (#1319).
#
# A bare `docker compose` auto-loads docker-compose.override.yml, the dev/test
# file that publishes the mock control port (50062) and the gRPC ports on all
# interfaces. On a venue network that lets anyone add or kill mock controllers
# and call services directly, so every compose call on the Pi path names its
# files with -f.
#
# 1. Static: the Pi scripts (scripts/setup/*, scripts/hardware/*.sh) contain no
#    `docker compose` call without -f (comments and echoed hints ignored).
# 2. Behaviour: joustmania-start.sh is run against a stub docker; every compose
#    call it makes starts with `-f docker-compose.yml --env-file .env`, plus
#    `--env-file .env.local` (last, so it wins) exactly when .env.local exists
#    (#1488); it ends in `up -d`, never prints the env values, and removes a
#    stale joustmania-python-hid container only while the python-hid profile
#    is inactive (#1402 I1). The env files are dummies created by the test.
# Runs as part of `make lint`, or directly:
#   ./scripts/tests/test-pi-start-compose-files.sh
# UC: UC-035 BR-001
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}

# --- 1. static check -------------------------------------------------------
# A `docker compose` invocation on a non-comment, non-echo line that is not
# immediately followed by -f or a files array ("${COMPOSE_FILES[@]}").
BARE_COMPOSE='^[^#]*docker compose( |\)|$)'
NAMED_FILES='docker compose (-f |"\$\{COMPOSE_FILES\[@\]\}")'
is_bare() {
  local line="$1"
  [[ "${line}" =~ ^[[:space:]]*# ]] && return 1
  [[ "${line}" =~ ^[[:space:]]*echo ]] && return 1
  # `docker compose version` and `command -v docker compose` read no compose file
  line="$(sed -E 's/docker compose version//g; s/command -v docker compose//g' <<<"${line}")"
  grep -qE "${BARE_COMPOSE}" <<<"${line}" || return 1
  ! grep -qE "${NAMED_FILES}" <<<"${line}"
}

for script in "${ROOT}"/scripts/setup/*.sh "${ROOT}"/scripts/setup/*.service "${ROOT}"/scripts/hardware/*.sh; do
  [[ -f "${script}" ]] || continue
  n=0
  while IFS= read -r line; do
    n=$((n + 1))
    if is_bare "${line}"; then
      fail "${script#"${ROOT}/"}:${n} runs compose without -f (loads the dev override): ${line}"
    fi
  done <"${script}"
done

# The check itself must catch the old lines and pass the fixed ones
for bad in 'exec docker compose up -d' 'if docker compose pull 2>&1; then' \
  'ExecStop=/usr/bin/docker compose down' '    docker compose down --remove-orphans || true' \
  'COMPOSE=(docker compose)' 'COMPOSE=(docker compose); "${COMPOSE[@]}" up -d'; do
  is_bare "${bad}" || fail "self-test: bare compose not flagged: ${bad}"
done
for good in 'exec docker compose "${COMPOSE_FILES[@]}" up -d' \
  'ExecStop=/usr/bin/docker compose -f docker-compose.yml down' \
  '# a bare `docker compose` would load the override' \
  'echo "You can start it with: docker compose up -d"' \
  'if ! command -v docker compose &> /dev/null && ! docker compose version &> /dev/null; then'; do
  if is_bare "${good}"; then fail "self-test: wrongly flagged: ${good}"; fi
done

# --- 2. behaviour of joustmania-start.sh -----------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

DUMMY_SECRET="dummy-not-a-key-1488"

run_start() { # $1 = services printed by `compose config --services`, $2 = case, $3 = with-env-local|no-env-local
  local case_dir="${WORK}/$2"
  mkdir -p "${case_dir}/repo/scripts/setup" "${case_dir}/bin"
  cp "${ROOT}/scripts/setup/joustmania-start.sh" "${case_dir}/repo/scripts/setup/"
  if [[ -f "${ROOT}/scripts/setup/joustmania-compose.sh" ]]; then
    cp "${ROOT}/scripts/setup/joustmania-compose.sh" "${case_dir}/repo/scripts/setup/"
  fi
  printf 'IMAGE_TAG=0.0.0-test\n' >"${case_dir}/repo/.env"
  if [[ "$3" == with-env-local ]]; then
    printf 'IMAGE_TAG=stage-test\nJEV_API_KEY=%s\nANTHROPIC_API_KEY=%s\n' "${DUMMY_SECRET}" "${DUMMY_SECRET}" >"${case_dir}/repo/.env.local"
  fi
  : >"${case_dir}/docker.log"
  cat >"${case_dir}/bin/docker" <<STUB
#!/usr/bin/env bash
echo "\$*" >>"${case_dir}/docker.log"
if [[ "\$1" == compose && " \$* " == *" config --services "* ]]; then
  printf '%s\n' ${1}
fi
exit 0
STUB
  printf '#!/usr/bin/env bash\nexit 1\n' >"${case_dir}/bin/git"     # offline: pull fails, non-fatal
  printf '#!/usr/bin/env bash\nexit 0\n' >"${case_dir}/bin/amixer"
  chmod +x "${case_dir}/bin/"*
  PATH="${case_dir}/bin:${PATH}" JOUSTMANIA_STATUS_DIR="${case_dir}/status" COMPOSE_PROFILES= \
    bash "${case_dir}/repo/scripts/setup/joustmania-start.sh" >"${case_dir}/out.log" 2>&1 \
    || fail "$2: joustmania-start.sh exited non-zero (run the test locally to see its output)"
  if grep -rq "${DUMMY_SECRET}" "${case_dir}/out.log" "${case_dir}/status" 2>/dev/null; then
    fail "$2: the start script printed an env value"
  fi
}

check_compose_calls() { # $1 = docker.log, $2 = case name, $3 = expected compose prefix
  local calls
  calls="$(grep '^compose' "$1" || true)"
  [[ -n "${calls}" ]] || fail "$2: no compose call recorded"
  while IFS= read -r call; do
    [[ -z "${call}" ]] && continue
    [[ "${call}" == "$3 "* ]] || fail "$2: compose call does not start with '$3': docker ${call}"
    if [[ "${call}" == *override* ]]; then fail "$2: compose call loads the override: docker ${call}"; fi
  done <<<"${calls}"
  [[ "$(tail -1 "$1")" == "$3 up -d" ]] \
    || fail "$2: last docker call is not '$3 up -d': $(tail -1 "$1")"
}

BASE_PREFIX="compose -f docker-compose.yml --env-file .env"

# No .env.local, python-hid profile inactive: only .env; the stale container is removed
run_start "flagd rust-hid controller-manager" no-profile no-env-local
log="${WORK}/no-profile/docker.log"
check_compose_calls "${log}" no-profile "${BASE_PREFIX}"
if grep -q 'env.local' "${log}"; then fail "no-profile: .env.local passed although it does not exist"; fi
grep -qx 'rm -f joustmania-python-hid' "${log}" \
  || fail "no-profile: stale joustmania-python-hid not removed"

# .env.local present, python-hid profile active: .env then .env.local (later
# wins) on every call; the container is left to compose
run_start "flagd rust-hid python-hid controller-manager" python-profile with-env-local
log="${WORK}/python-profile/docker.log"
check_compose_calls "${log}" python-profile "${BASE_PREFIX} --env-file .env.local"
grep -q 'Env files: .env + .env.local' "${WORK}/python-profile/out.log" \
  || fail "python-profile: the start script does not say .env.local was loaded"
if grep -qx 'rm -f joustmania-python-hid' "${log}"; then
  fail "python-profile: joustmania-python-hid removed although its profile is active"
fi

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: the Pi start path names its compose and env files and never loads the dev override"
