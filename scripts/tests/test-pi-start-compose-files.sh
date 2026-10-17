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
#    call it makes starts with `-f docker-compose.yml`, it ends in `up -d`, and
#    it removes a stale joustmania-python-hid container only while the
#    python-hid profile is inactive (#1402 I1).
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
BARE_COMPOSE='^[^#]*docker compose( |$)'
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
  'ExecStop=/usr/bin/docker compose down' '    docker compose down --remove-orphans || true'; do
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

run_start() { # $1 = services printed by `compose config --services`
  local case_dir="${WORK}/$2"
  mkdir -p "${case_dir}/repo/scripts/setup" "${case_dir}/bin"
  cp "${ROOT}/scripts/setup/joustmania-start.sh" "${case_dir}/repo/scripts/setup/"
  : >"${case_dir}/docker.log"
  cat >"${case_dir}/bin/docker" <<EOF
#!/usr/bin/env bash
echo "\$*" >>"${case_dir}/docker.log"
if [[ "\$1" == compose && " \$* " == *" config --services "* ]]; then
  printf '%s\n' ${1}
fi
exit 0
EOF
  printf '#!/usr/bin/env bash\nexit 1\n' >"${case_dir}/bin/git"     # offline: pull fails, non-fatal
  printf '#!/usr/bin/env bash\nexit 0\n' >"${case_dir}/bin/amixer"
  chmod +x "${case_dir}/bin/"*
  PATH="${case_dir}/bin:${PATH}" JOUSTMANIA_STATUS_DIR="${case_dir}/status" COMPOSE_PROFILES= \
    bash "${case_dir}/repo/scripts/setup/joustmania-start.sh" >"${case_dir}/out.log" 2>&1 \
    || fail "$2: joustmania-start.sh exited non-zero: $(tail -3 "${case_dir}/out.log")"
  echo "${case_dir}/docker.log"
}

check_compose_calls() { # $1 = docker.log, $2 = case name
  local calls
  calls="$(grep '^compose' "$1" || true)"
  [[ -n "${calls}" ]] || fail "$2: no compose call recorded"
  while IFS= read -r call; do
    [[ -z "${call}" ]] && continue
    [[ "${call}" == "compose -f docker-compose.yml "* ]] \
      || fail "$2: compose call without -f docker-compose.yml: docker ${call}"
    if [[ "${call}" == *override* ]]; then fail "$2: compose call loads the override: docker ${call}"; fi
  done <<<"${calls}"
  [[ "$(tail -1 "$1")" == "compose -f docker-compose.yml up -d" ]] \
    || fail "$2: last docker call is not 'compose -f docker-compose.yml up -d': $(tail -1 "$1")"
}

# python-hid profile inactive: the stale container is removed
log="$(run_start "flagd rust-hid controller-manager" no-profile)"
check_compose_calls "${log}" no-profile
grep -qx 'rm -f joustmania-python-hid' "${log}" \
  || fail "no-profile: stale joustmania-python-hid not removed"

# python-hid profile active: the container is left to compose
log="$(run_start "flagd rust-hid python-hid controller-manager" python-profile)"
check_compose_calls "${log}" python-profile
if grep -qx 'rm -f joustmania-python-hid' "${log}"; then
  fail "python-profile: joustmania-python-hid removed although its profile is active"
fi

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: the Pi start path names its compose files and never loads the dev override"
