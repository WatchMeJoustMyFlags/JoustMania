#!/usr/bin/env bash
# The setup scripts that ./setup.sh runs on a new Pi (UC-060 A6, A7; #2068, #2027).
#
# setup_runtime.sh (A6 step 1) runs with stub sudo / docker / logname / lsb_release /
# amixer / curl on PATH and fake config.txt and input.conf files
# (JOUSTMANIA_BOOT_CONFIG, JOUSTMANIA_BT_INPUT_CONF); nothing real is installed:
#  - the order of the privileged steps: apt update, apt install of the
#    Bluetooth/audio dependencies, the Docker log defaults, then the
#    Bluetooth config
#  - a fresh Pi: dtoverlay=disable-bt is appended once and a reboot is asked
#    for; input.conf is created with ClassicBondedOnly=false
#  - a rerun changes nothing and asks for no reboot; ClassicBondedOnly=true is
#    switched to false and a file without the key gets it appended
#  - Docker already installed: no get.docker.com download, no docker group change
#  - a failed apt install exits non-zero before the log defaults and the Bluetooth
#    config
# setup_host.sh (A7) is not run (it works in the real home directory and builds a
# virtualenv there); its steps are checked in the source: nine numbered steps in
# order, fail-fast, root only through sudo, the pairing daemon not installed on the
# host. update_permissions.sh runs in a scratch directory with a stub sudo: it
# changes the owner of the current directory, recursively, to the calling user.
# Runs as part of `make lint`, or directly: ./scripts/tests/test-setup-scripts.sh
# UC: UC-060 A6 A7
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}
if [[ "${EUID}" -eq 0 ]]; then
  echo "skip: setup_runtime.sh refuses root, run this test as a normal user"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
BIN="${WORK}/bin"
mkdir -p "${BIN}"
CALLS="${WORK}/calls.log"

stub() { # stub <name> <body...>
  printf '#!/usr/bin/env bash\n%s\n' "${*:2}" >"${BIN}/$1"
  chmod +x "${BIN}/$1"
}
# sudo: log the command; only tee / sed run, and only on the fake files (the paths
# are overridden); apt-get fails when ${WORK}/apt-rc says so
stub sudo 'echo "sudo $*" >>"'"${CALLS}"'"
case "$1" in
  tee | sed) exec "$@" ;;
  apt-get) [[ "$2" == install ]] && exit "$(cat "'"${WORK}"'/apt-rc")" ;;
esac
exit 0'
stub logname 'echo tester'
stub lsb_release 'echo 12'
stub amixer 'exit 0'
stub curl 'echo "curl $*" >>"'"${CALLS}"'"; exit 1'
stub docker 'exit 0'
export PATH="${BIN}:${PATH}"

# run_runtime <case>: fresh files in ${WORK}/<case>; output in ${WORK}/<case>/out
run_runtime() {
  local dir="${WORK}/$1"
  mkdir -p "${dir}"
  : >"${CALLS}"
  echo 0 >"${WORK}/apt-rc"
  RC=0
  JOUSTMANIA_BOOT_CONFIG="${dir}/config.txt" JOUSTMANIA_BT_INPUT_CONF="${dir}/input.conf" \
    bash "${ROOT}/scripts/setup/setup_runtime.sh" >"${dir}/out" 2>&1 || RC=$?
}
# line_of <pattern>: line number of the first call matching it, 0 when none
line_of() { grep -n -m1 -- "$1" "${CALLS}" | cut -d: -f1 || true; }

# --- fresh Pi
mkdir -p "${WORK}/fresh"
: >"${WORK}/fresh/config.txt"
run_runtime fresh
d="${WORK}/fresh"
[[ "${RC}" -eq 0 ]] || fail "fresh: exit ${RC}"
grep -qx 'dtoverlay=disable-bt' "${d}/config.txt" || fail "fresh: disable-bt not appended"
grep -q 'Reboot to disable internal Bluetooth' "${d}/out" || fail "fresh: no reboot request"
[[ "$(cat "${d}/input.conf")" == $'[General]\nClassicBondedOnly=false' ]] || fail "fresh: input.conf not created"
u="$(line_of 'sudo apt-get update')"
i="$(line_of 'sudo apt-get install -y bluez bluez-tools alsa-utils jq')"
l="$(line_of 'sudo bash .*docker-log-defaults.sh')"
t="$(line_of 'sudo tee -a')"
if ! [[ -n "${u}" && -n "${i}" && -n "${l}" && -n "${t}" && "${u}" -lt "${i}" && "${i}" -lt "${l}" && "${l}" -lt "${t}" ]]; then
  fail "fresh: step order wrong (update=${u} install=${i} log-defaults=${l} bluetooth=${t})"
fi
grep -q 'curl\|usermod' "${CALLS}" && fail "fresh: Docker is installed, yet curl/usermod ran"

# --- rerun on the result: nothing changes
cp "${d}/config.txt" "${d}/config.before"
cp "${d}/input.conf" "${d}/input.before"
run_runtime fresh
[[ "${RC}" -eq 0 ]] || fail "rerun: exit ${RC}"
cmp -s "${d}/config.txt" "${d}/config.before" || fail "rerun: config.txt changed"
cmp -s "${d}/input.conf" "${d}/input.before" || fail "rerun: input.conf changed"
[[ "$(grep -c disable-bt "${d}/config.txt")" -eq 1 ]] || fail "rerun: disable-bt duplicated"
grep -q 'Reboot to disable' "${d}/out" && fail "rerun: asks for a reboot"
grep -q 'Internal Bluetooth already disabled' "${d}/out" || fail "rerun: no 'already disabled'"

# --- ClassicBondedOnly=true is switched; a file without the key gets it
mkdir -p "${WORK}/true" "${WORK}/nokey"
: >"${WORK}/true/config.txt"
printf '[General]\nClassicBondedOnly=true\nOther=1\n' >"${WORK}/true/input.conf"
run_runtime true
[[ "$(cat "${WORK}/true/input.conf")" == $'[General]\nClassicBondedOnly=false\nOther=1' ]] || fail "true: not switched, or other keys lost"
: >"${WORK}/nokey/config.txt"
printf '[General]\nOther=1\n' >"${WORK}/nokey/input.conf"
run_runtime nokey
[[ "$(cat "${WORK}/nokey/input.conf")" == $'[General]\nOther=1\nClassicBondedOnly=false' ]] || fail "nokey: key not appended"

# --- a failed apt install stops before the log defaults and the Bluetooth config
mkdir -p "${WORK}/aptfail"
: >"${WORK}/aptfail/config.txt"
: >"${CALLS}"
echo 1 >"${WORK}/apt-rc"
RC=0
JOUSTMANIA_BOOT_CONFIG="${WORK}/aptfail/config.txt" JOUSTMANIA_BT_INPUT_CONF="${WORK}/aptfail/input.conf" \
  bash "${ROOT}/scripts/setup/setup_runtime.sh" >"${WORK}/aptfail/out" 2>&1 || RC=$?
[[ "${RC}" -ne 0 ]] || fail "aptfail: exit 0"
grep -q 'docker-log-defaults' "${CALLS}" && fail "aptfail: log defaults ran after the failure"
[[ -s "${WORK}/aptfail/config.txt" ]] && fail "aptfail: config.txt written after the failure"
[[ -e "${WORK}/aptfail/input.conf" ]] && fail "aptfail: input.conf written after the failure"

# --- setup_host.sh (A7): static checks, it is not run
host="${ROOT}/scripts/setup/setup_host.sh"
prev=0
for n in 1 2 3 4 5 6 7 8 9; do
  ln="$(grep -n "^echo \"\[${n}/9\]" "${host}" | head -1 | cut -d: -f1 || true)"
  if [[ -z "${ln}" || "${ln}" -le "${prev}" ]]; then
    fail "setup_host.sh: step [${n}/9] missing or out of order"
  else
    prev="${ln}"
  fi
done
grep -q '^set -e' "${host}" || fail "setup_host.sh: not fail-fast (set -e)"
grep -q 'dtoverlay=disable-bt' "${host}" || fail "setup_host.sh: internal Bluetooth not disabled"
grep -q 'ClassicBondedOnly=false' "${host}" || fail "setup_host.sh: ClassicBondedOnly not set"
grep -qE 'get\.docker\.com' "${host}" || fail "setup_host.sh: Docker install missing"
# every apt-get / systemctl / chown / cp to /etc goes through sudo
if grep -nE '^[[:space:]]*(apt-get|systemctl|chown|usermod|alsactl)[[:space:]]' "${host}"; then
  fail "setup_host.sh: a privileged command without sudo"
fi
grep -qE 'apt-get install.*pairing|systemctl (enable|start).*pairing' "${host}" && fail "setup_host.sh: installs the pairing daemon on the host"

# --- update_permissions.sh (A7): chown -R of the current directory to $USER, via sudo
: >"${CALLS}"
mkdir -p "${WORK}/checkout"
(cd "${WORK}/checkout" && USER=tester bash "${ROOT}/scripts/hardware/update_permissions.sh")
[[ "$(cat "${CALLS}")" == "sudo chown -R tester:tester ." ]] || fail "update_permissions.sh: unexpected calls: $(cat "${CALLS}")"

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: setup scripts (A6 runtime steps, A7 host steps, update_permissions)"
