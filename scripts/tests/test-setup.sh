#!/usr/bin/env bash
# First-time install of a Pi with ./setup.sh (UC-060 A6, C-040, C-041; #2027 PR 5).
#
# setup.sh runs in a scratch git checkout with stub setup scripts, sudo, docker
# and upgrade command; nothing real is installed or pulled:
#  - a failed setup script (runtime or host) exits non-zero and offers nothing
#    after it: no autostart, no pin, no .env.local, no reboot
#  - .env.local is created from .env.example with mode 600 and no IMAGE_TAG; an
#    existing one is left byte-identical
#  - with the autostart installed, the pin runs joustmania-upgrade.sh (it pulls
#    the images while online, so the first boot needs no network): the newest
#    release that contains the pinnable floor, else HEAD's full sha, else the
#    version a pinned checkout already has
#  - docker not usable yet (new docker group): nothing pulled, the command is
#    listed under "Still to do" with the A4 warning, exit 0; the steps listed
#    are the same as with docker usable
#  - a failed upgrade exits non-zero; declining the pin says the autostart
#    refuses a branch (A4)
#  - without the autostart a pinned checkout gets `joustmania-compose.sh pull`
#  - A6 step 6: setup.sh refuses to run as root; a checkout the user does not own
#    names scripts/hardware/update_permissions.sh; both exit 1 before any step
#  - a pinned checkout with the autostart keeps its own version (A6 step 4)
# Runs as part of `make lint`, or directly: ./scripts/tests/test-setup.sh
# UC: UC-060 A6 A4
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fails=0
fail() {
  echo "FAIL $*"
  fails=$((fails + 1))
}
if [[ "${EUID}" -eq 0 ]]; then
  echo "skip: setup.sh refuses root, run this test as a normal user"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
g() { git -c user.name=t -c user.email=t@t -c commit.gpgsign=false -c tag.gpgsign=false "$@"; }

# make_case <name> [no-release]: ${WORK}/<name>/{repo,bin,calls.log}; sets CASE.
# History: v0.7.0 (below the floor), the floor commit, v0.8.0 (unless
# no-release), then HEAD on branch main.
#   $CASE/runtime-rc, host-rc, upgrade-rc: exit codes of those stubs
#   $CASE/docker-down: `docker info` fails
make_case() {
  CASE="${WORK}/$1"
  local repo="${CASE}/repo" floor s
  mkdir -p "${repo}/scripts/setup" "${CASE}/bin"
  cp "${ROOT}/setup.sh" "${ROOT}/.env.example" "${repo}/"
  cp "${ROOT}/scripts/setup/lib-joustmania.sh" "${repo}/scripts/setup/"
  : >"${CASE}/calls.log"
  echo 0 >"${CASE}/runtime-rc"
  echo 0 >"${CASE}/host-rc"
  echo 0 >"${CASE}/upgrade-rc"
  g -C "${repo}" init -q -b main
  g -C "${repo}" commit -q --allow-empty -m old
  g -C "${repo}" tag joustmania-v0.7.0
  g -C "${repo}" commit -q --allow-empty -m floor
  floor="$(g -C "${repo}" rev-parse HEAD)"
  g -C "${repo}" commit -q --allow-empty -m release
  [[ "${2:-}" == no-release ]] || g -C "${repo}" tag joustmania-v0.8.0
  g -C "${repo}" commit -q --allow-empty -m head
  for s in setup_runtime setup_host install_autostart joustmania-upgrade joustmania-compose; do
    {
      printf '#!/usr/bin/env bash\n'
      [[ "$s" == joustmania-upgrade ]] && printf 'MIN_PINNABLE_COMMIT="%s"\n' "${floor}"
      printf 'echo "%s $*" >>"%s/calls.log"\n' "$s" "${CASE}"
      case "$s" in
        setup_runtime) printf 'exit "$(cat "%s/runtime-rc")"\n' "${CASE}" ;;
        setup_host) printf 'exit "$(cat "%s/host-rc")"\n' "${CASE}" ;;
        joustmania-upgrade) printf 'exit "$(cat "%s/upgrade-rc")"\n' "${CASE}" ;;
      esac
    } >"${repo}/scripts/setup/$s.sh"
  done
  printf '#!/usr/bin/env bash\necho "sudo $*" >>"%s/calls.log"\nexec "$@"\n' "${CASE}" >"${CASE}/bin/sudo"
  printf '#!/usr/bin/env bash\necho "reboot" >>"%s/calls.log"\n' "${CASE}" >"${CASE}/bin/reboot"
  printf '#!/usr/bin/env bash\n[[ "$1" == info && -e "%s/docker-down" ]] && exit 1\nexit 0\n' "${CASE}" >"${CASE}/bin/docker"
  chmod +x "${CASE}/bin/"* "${repo}/scripts/setup/"*.sh
}

# run_setup <keys>: the answers, one key each; returns setup.sh's exit code
run_setup() {
  (cd "${CASE}" && printf '%s' "$1" | env PATH="${CASE}/bin:${PATH}" bash "${CASE}/repo/setup.sh" >"${CASE}/out.log" 2>&1)
}
called() { grep -q "^$1" "${CASE}/calls.log"; }

# --- 1. the runtime setup fails: exit non-zero, nothing offered after it ------
make_case runtime-fails
echo 1 >"${CASE}/runtime-rc"
if run_setup 1yyy; then fail "runtime-fails: setup.sh exited 0"; fi
called install_autostart && fail "runtime-fails: autostart offered after a failed setup"
called joustmania-upgrade && fail "runtime-fails: pin offered after a failed setup"
[[ -e "${CASE}/repo/.env.local" ]] && fail "runtime-fails: .env.local created after a failed setup"
grep -q 'Runtime setup failed' "${CASE}/out.log" || fail "runtime-fails: no error message"

# --- 2. the host setup fails: exit non-zero, no reboot ----------------------
make_case host-fails
echo 1 >"${CASE}/host-rc"
if run_setup 2yyyy; then fail "host-fails: setup.sh exited 0"; fi
called install_autostart && fail "host-fails: autostart offered after a failed setup"
called reboot && fail "host-fails: rebooted"
grep -q 'will reboot' "${CASE}/out.log" && fail "host-fails: a reboot was scheduled"

# --- 3. happy path: .env.local, autostart, pin to the newest pinnable release
make_case happy
run_setup 1yyy || fail "happy: setup.sh failed: $(tail -5 "${CASE}/out.log")"
[[ "$(stat -c %a "${CASE}/repo/.env.local" 2>/dev/null)" == 600 ]] || fail "happy: .env.local is not mode 600"
grep -qE '^[[:space:]]*(export[[:space:]]+)?IMAGE_TAG' "${CASE}/repo/.env.local" && fail "happy: .env.local sets IMAGE_TAG"
called 'sudo .*/install_autostart.sh' || fail "happy: install_autostart.sh not run with sudo: $(cat "${CASE}/calls.log")"
called 'joustmania-upgrade joustmania-v0.8.0$' || fail "happy: not pinned to joustmania-v0.8.0: $(cat "${CASE}/calls.log")"
[[ "$(grep -n install_autostart "${CASE}/calls.log" | head -n 1 | cut -d: -f1)" -lt "$(grep -n joustmania-upgrade "${CASE}/calls.log" | cut -d: -f1)" ]] \
  || fail "happy: the pin ran before the unit was installed (the upgrade restarts the unit)"
grep -q 'lite.yml is for manual tests only' "${CASE}/out.log" || fail "happy: the lite stack is not marked manual-only"
grep -q 'docker compose -f docker-compose.lite.yml up' "${CASE}/out.log" && fail "happy: the lite stack is offered as the way to start"

# --- 4. no release contains the floor: pin HEAD's full sha ------------------
make_case no-release no-release
run_setup 1yyy || fail "no-release: setup.sh failed: $(tail -5 "${CASE}/out.log")"
head_sha="$(git -C "${CASE}/repo" rev-parse HEAD)"
called "joustmania-upgrade ${head_sha}$" || fail "no-release: not pinned to HEAD's sha: $(cat "${CASE}/calls.log")"

# --- 5. an existing .env.local is kept (and not asked about) ----------------
make_case env-kept
printf 'JEV_API_KEY=keep-me\n' >"${CASE}/repo/.env.local"
chmod 640 "${CASE}/repo/.env.local"
run_setup 1yy || fail "env-kept: setup.sh failed: $(tail -5 "${CASE}/out.log")"
[[ "$(cat "${CASE}/repo/.env.local")" == "JEV_API_KEY=keep-me" ]] || fail "env-kept: .env.local changed"
called 'joustmania-upgrade joustmania-v0.8.0$' || fail "env-kept: the answers shifted, no pin: $(cat "${CASE}/calls.log")"
grep -q 'keep-me' "${CASE}/out.log" && fail "env-kept: an env value was printed"

# --- 6. docker not usable yet: nothing pulled, the command is listed --------
make_case docker-group
touch "${CASE}/docker-down"
run_setup 1yyy || fail "docker-group: setup.sh failed: $(tail -5 "${CASE}/out.log")"
called joustmania-upgrade && fail "docker-group: the upgrade ran without docker"
grep -A3 'Still to do' "${CASE}/out.log" | grep -q 'joustmania-upgrade.sh joustmania-v0.8.0' \
  || fail "docker-group: the pin command is not listed: $(cat "${CASE}/out.log")"
grep -q 'next boot starts nothing until you run the pin' "${CASE}/out.log" || fail "docker-group: no A4 warning for the deferred pin"

# --- 6b. no autostart on a branch: the same steps whether docker works or not
todo() { sed -n '/^Still to do:/,/^$/p' "${CASE}/out.log" | sed "s|${CASE}||g"; }
make_case later-docker-ok
run_setup 1yn || fail "later-docker-ok: setup.sh failed"
todo_ok="$(todo)"
make_case later-docker-down
touch "${CASE}/docker-down"
run_setup 1yn || fail "later-docker-down: setup.sh failed"
[[ -n "${todo_ok}" && "$(todo)" == "${todo_ok}" ]] || fail "later: the steps differ with docker usable or not: [${todo_ok}] vs [$(todo)]"
grep -q 'install_autostart.sh && .*joustmania-upgrade.sh joustmania-v0.8.0' <<<"${todo_ok}" || fail "later: autostart + pin not listed: ${todo_ok}"
called 'joustmania-compose pull' && fail "later-docker-down: a branch checkout was offered a pull of the .env tag"

# --- 7. the upgrade fails: exit non-zero ------------------------------------
make_case upgrade-fails
echo 1 >"${CASE}/upgrade-rc"
if run_setup 1yyy; then fail "upgrade-fails: setup.sh exited 0"; fi
grep -q 'first boot is not ready' "${CASE}/out.log" || fail "upgrade-fails: no error message"

# --- 8. the pin declined: the autostart would refuse the branch (A4) --------
make_case pin-declined
run_setup 1yyn || fail "pin-declined: setup.sh failed: $(tail -5 "${CASE}/out.log")"
called joustmania-upgrade && fail "pin-declined: the upgrade ran"
grep -q 'refuses to start a branch checkout' "${CASE}/out.log" || fail "pin-declined: no A4 warning"
grep -A3 'Still to do' "${CASE}/out.log" | grep -q 'joustmania-upgrade.sh joustmania-v0.8.0' || fail "pin-declined: the pin command is not listed"

# --- 9. no autostart, a pinned checkout: its images are pulled --------------
make_case pull-only
g -C "${CASE}/repo" checkout -q --detach joustmania-v0.8.0
run_setup 1yny || fail "pull-only: setup.sh failed: $(tail -5 "${CASE}/out.log")"
called install_autostart && fail "pull-only: autostart installed although declined"
called 'joustmania-compose pull$' || fail "pull-only: the images were not pulled: $(cat "${CASE}/calls.log")"
called joustmania-upgrade && fail "pull-only: the upgrade ran without the unit"

# --- 10. as root: refused (A6 step 6) ---------------------------------------
# The test cannot be root, so the guard's condition is made true in a copy
# (EUID cannot be set from outside); the message and the exit are the real ones.
make_case root-refused
sed -i 's/"\$EUID" -eq 0/0 -eq 0/' "${CASE}/repo/setup.sh"
cmp -s "${ROOT}/setup.sh" "${CASE}/repo/setup.sh" && fail "root-refused: the root guard was not found in setup.sh"
rc=0
run_setup 1yyy || rc=$?
[[ "${rc}" -eq 1 ]] || fail "root-refused: exit ${rc}, not 1"
grep -q 'without sudo' "${CASE}/out.log" || fail "root-refused: no refusal message: $(cat "${CASE}/out.log")"
[[ -s "${CASE}/calls.log" ]] && fail "root-refused: a step ran: $(cat "${CASE}/calls.log")"

# --- 11. a checkout the user does not own: update_permissions.sh is named ----
# `/` is owned by root, so `! -O /` is true for the test user; the rest is real.
make_case not-owner
sed -i 's|! -O "\$SCRIPT_DIR"|! -O /|' "${CASE}/repo/setup.sh"
cmp -s "${ROOT}/setup.sh" "${CASE}/repo/setup.sh" && fail "not-owner: the owner guard was not found in setup.sh"
rc=0
run_setup 1yyy || rc=$?
[[ "${rc}" -eq 1 ]] || fail "not-owner: exit ${rc}, not 1"
grep -q 'scripts/hardware/update_permissions.sh' "${CASE}/out.log" || fail "not-owner: update_permissions.sh not named: $(cat "${CASE}/out.log")"
[[ -s "${CASE}/calls.log" ]] && fail "not-owner: a step ran: $(cat "${CASE}/calls.log")"

# --- 12. a pinned checkout with the autostart keeps its own version ---------
# v0.7.0 is below the floor and v0.8.0 is the newest release: the pin must stay v0.7.0
make_case pinned-kept
g -C "${CASE}/repo" checkout -q --detach joustmania-v0.7.0
run_setup 1yyy || fail "pinned-kept: setup.sh failed: $(tail -5 "${CASE}/out.log")"
called 'joustmania-upgrade joustmania-v0.7.0$' || fail "pinned-kept: not kept at joustmania-v0.7.0: $(cat "${CASE}/calls.log")"
called 'joustmania-upgrade joustmania-v0.8.0' && fail "pinned-kept: moved to the newest release"

if [[ "${fails}" -ne 0 ]]; then
  exit 1
fi
echo "ok: setup.sh stops on a failed step, creates a private .env.local and pins the version with its images"
