#!/usr/bin/env bash
# Docker's default log limit on the Pi (UC-060 A6; #2027, audit finding 8).
#
# scripts/setup/docker-log-defaults.sh against a scratch daemon.json and a stub
# systemctl (no real docker restart):
#  - no file: written with json-file, max-size 10m, max-file 3; docker restarted
#  - an existing file: the defaults merged in, every key already there wins
#    (nested log-opts too); docker restarted only when the file changed
#  - a second run changes nothing and restarts nothing
#  - another log driver, or a file that is not a JSON object: left
#    byte-identical, no restart
# Runs as part of `make lint`, or directly: ./scripts/tests/test-docker-log-defaults.sh
# UC: UC-060 A6
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="${ROOT}/scripts/setup/docker-log-defaults.sh"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}
if ! command -v jq >/dev/null 2>&1; then
  echo "skip: jq is not installed"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
printf '#!/usr/bin/env bash\necho "$*" >>"%s/systemctl.log"\n' "${WORK}" >"${WORK}/bin/systemctl"
chmod +x "${WORK}/bin/systemctl"

# run <case>: the script against ${WORK}/<case>/daemon.json; resets the restart log
run() {
  : >"${WORK}/systemctl.log"
  env PATH="${WORK}/bin:${PATH}" DOCKER_DAEMON_JSON="${WORK}/$1/daemon.json" bash "${SCRIPT}" >"${WORK}/$1/out.log" 2>&1
}
restarted() { grep -qx 'restart docker' "${WORK}/systemctl.log"; }
opt() { jq -r "$2" "${WORK}/$1/daemon.json"; }

# --- 1. no daemon.json: written, docker restarted -----------------------------
mkdir -p "${WORK}/new/etc"
DOCKER_DAEMON_JSON="${WORK}/new/etc/daemon.json"
: >"${WORK}/systemctl.log"
env PATH="${WORK}/bin:${PATH}" DOCKER_DAEMON_JSON="${DOCKER_DAEMON_JSON}" bash "${SCRIPT}" >"${WORK}/new/out.log" 2>&1 || fail "new: exited non-zero"
[[ "$(jq -c . "${DOCKER_DAEMON_JSON}")" == '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' ]] \
  || fail "new: wrong defaults: $(cat "${DOCKER_DAEMON_JSON}")"
restarted || fail "new: docker not restarted after writing the file"

# --- 2. an existing file: merged, existing keys win ---------------------------
mkdir -p "${WORK}/merge"
printf '{"bip":"172.17.0.1/16","log-opts":{"max-size":"50m"}}\n' >"${WORK}/merge/daemon.json"
run merge || fail "merge: exited non-zero"
[[ "$(opt merge '.bip')" == 172.17.0.1/16 ]] || fail "merge: an existing key was lost"
[[ "$(opt merge '."log-opts"."max-size"')" == 50m ]] || fail "merge: the existing max-size was overwritten"
[[ "$(opt merge '."log-opts"."max-file"')" == 3 ]] || fail "merge: max-file not added"
[[ "$(opt merge '."log-driver"')" == json-file ]] || fail "merge: log-driver not added"
restarted || fail "merge: docker not restarted after a change"

# --- 3. a second run: unchanged, no restart -----------------------------------
before="$(cat "${WORK}/merge/daemon.json")"
run merge || fail "rerun: exited non-zero"
[[ "$(cat "${WORK}/merge/daemon.json")" == "${before}" ]] || fail "rerun: the file changed"
restarted && fail "rerun: docker restarted although nothing changed"

# --- 4. another log driver: left alone -----------------------------------------
mkdir -p "${WORK}/journald"
printf '{"log-driver":"journald"}\n' >"${WORK}/journald/daemon.json"
before="$(cat "${WORK}/journald/daemon.json")"
run journald || fail "journald: exited non-zero"
[[ "$(cat "${WORK}/journald/daemon.json")" == "${before}" ]] || fail "journald: the file changed (json-file options would stop docker)"
restarted && fail "journald: docker restarted"

# --- 5. not a JSON object: left alone, WARN -----------------------------------
mkdir -p "${WORK}/broken"
printf '{"bip": \n' >"${WORK}/broken/daemon.json"
before="$(cat "${WORK}/broken/daemon.json")"
run broken || fail "broken: exited non-zero"
[[ "$(cat "${WORK}/broken/daemon.json")" == "${before}" ]] || fail "broken: the file changed"
grep -q 'WARN' "${WORK}/broken/out.log" || fail "broken: no WARN"
restarted && fail "broken: docker restarted"

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: docker-log-defaults.sh writes or merges the json-file log limit and keeps the operator's keys"
