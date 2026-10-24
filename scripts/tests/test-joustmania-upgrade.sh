#!/usr/bin/env bash
# The Pi upgrade command and the pin (UC-060 A2, A3, A4, BR-003; FR-182, C-040; #2027 PR 3).
#
# scripts/setup/joustmania-upgrade.sh runs in a throwaway clone of a throwaway
# repository (real git with a local bare origin, wrapped by a logging shim) and
# against stub docker, curl, systemctl, sudo and id. Nothing reaches the network,
# the real docker stack or systemd.
#  - release tag and exact sha accepted; latest, main, short sha refused; below
#    MIN_PINNABLE (version and commit floor) refused; nothing changes on a refusal
#  - registry down (no 200/401) -> no fetch, no checkout, no restart
#  - prepare pull missing an image -> listed, nothing changed, temp worktree removed
#  - healthy -> switch, restart, record, prune (only superseded tags of our
#    repositories; an unrelated image survives); fail-closed prune without a record
#  - not healthy -> automatic rollback; --no-rollback leaves it; --rollback offline
#  - a stale worktree registration is pruned; COMPOSE_PROFILES from the unit honoured
#  - joustmania-compose.sh derives IMAGE_TAG from a pinned HEAD over .env.local
#    (X.Y.Z of the release tag, else the sha; nothing on a branch); the start
#    script WARNs about an IMAGE_TAG in .env.local
# Runs as part of `make lint`, or directly: ./scripts/tests/test-joustmania-upgrade.sh
# UC: UC-060 A2 A3 A4 BR-003
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unset COMPOSE DOCKER COMPOSE_PROJECT_NAME COMPOSE_PROFILES OTEL_COLLECTOR_TAG IMAGE_TAG \
  JOUSTMANIA_UNIT_REV RUNTIME_DIRECTORY GIT_DIR GIT_WORK_TREE || true
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
# Isolate git from the user's config (signing, hooks, default branch)
export HOME="${WORK}/home" GIT_CONFIG_NOSYSTEM=1 GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid \
  GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
mkdir -p "${HOME}"
g() { "${REAL_GIT}" -c init.defaultBranch=main -c commit.gpgsign=false -c tag.gpgsign=false "$@"; }

# --- the origin: old (pre-floor) -> floor (#2040 stand-in) -> 0.9.0 -> 1.0.0 -> 1.1.0 -> main
SRC="${WORK}/src"
g init -q "${SRC}"
commit() { # $1 = message, $2 = IMAGE_TAG in .env
  printf 'IMAGE_TAG=%s\n' "$2" >"${SRC}/.env"
  echo "$1" >"${SRC}/marker"
  g -C "${SRC}" add -A
  g -C "${SRC}" commit -q -m "$1"
  g -C "${SRC}" rev-parse HEAD
}
add_scripts() { # $1 = floor commit
  mkdir -p "${SRC}/scripts/setup"
  cp "${ROOT}/scripts/setup/joustmania-compose.sh" "${ROOT}/scripts/setup/lib-joustmania.sh" \
    "${ROOT}/scripts/setup/joustmania-start.sh" "${ROOT}/scripts/setup/joustmania.service" "${SRC}/scripts/setup/"
  sed -e "s/^MIN_PINNABLE_COMMIT=.*/MIN_PINNABLE_COMMIT=\"$1\"/" -e 's/^MIN_PINNABLE=""/MIN_PINNABLE="1.0.0"/' \
    "${ROOT}/scripts/setup/joustmania-upgrade.sh" >"${SRC}/scripts/setup/joustmania-upgrade.sh"
  chmod +x "${SRC}/scripts/setup/"*.sh
  printf '.env.local\n.joustmania-release\n.joustmania-release.tmp\n' >"${SRC}/.gitignore"
  : >"${SRC}/docker-compose.yml"
}
C_OLD="$(commit old 0.1.0)"
g -C "${SRC}" tag joustmania-v0.1.0
C_FLOOR="$(commit floor 0.1.0)"
add_scripts "${C_FLOOR}"
C_09="$(commit v0.9.0 0.9.0)"
g -C "${SRC}" tag joustmania-v0.9.0
C_V1="$(commit v1.0.0 1.0.0)"
g -C "${SRC}" tag joustmania-v1.0.0
C_V11="$(commit v1.1.0 1.1.0)"
g -C "${SRC}" tag joustmania-v1.1.0
C_MAIN="$(commit main-later 1.1.0)"
ORIGIN="${WORK}/origin.git"
g clone -q --bare "${SRC}" "${ORIGIN}"
: "${C_OLD}" "${C_09}"

GC=ghcr.io/o/joustmania/game-coordinator-service
AG=ghcr.io/o/joustmania/agent

# make_case <name> [<checkout ref>]: a clone at <ref> (default: branch main at
# 1.0.0, the pre-migration Pi) plus stubs; sets CASE and REPO
make_case() {
  CASE="${WORK}/case-$1"
  REPO="${CASE}/repo"
  mkdir -p "${CASE}/bin" "${CASE}/tmp"
  g clone -q "${ORIGIN}" "${REPO}"
  g -C "${REPO}" checkout -q -B main "${C_V1}"
  [[ -n "${2:-}" ]] && g -C "${REPO}" checkout -q --detach "$2"
  printf 'IMAGE_TAG=stale-local\nJEV_API_KEY=dummy-secret-2027\n' >"${REPO}/.env.local"
  printf '%s:1.0.0\n%s:1.0.0\n%s:0.9.0\ngrafana/grafana:13\ngrafana/grafana:12\nbusybox:latest\n' \
    "${GC}" "${AG}" "${GC}" >"${CASE}/images.local"
  printf 'flag-service\ngame-coordinator\nmenu\njaeger\n' >"${CASE}/services"
  echo 200 >"${CASE}/http_code"
  echo 0 >"${CASE}/restart_rc"
  : >"${CASE}/unit-env"
  : >"${CASE}/calls.log"
  # docker: IMAGE_TAG from the shell (the wrapper's pin export) else the --env-file files
  cat >"${CASE}/bin/docker" <<STUB
#!/usr/bin/env bash
C="${CASE}"
tag="\${IMAGE_TAG:-}"
if [[ -z "\$tag" ]]; then
  prev=""
  for a in "\$@"; do
    [[ "\$prev" == --env-file ]] && { t="\$(sed -n 's/^IMAGE_TAG=//p' "\$a" | tail -n 1)"; [[ -n "\$t" ]] && tag="\$t"; }
    prev="\$a"
  done
fi
echo "docker \$* | tag=\$tag profiles=\${COMPOSE_PROFILES:-}" >>"\$C/calls.log"
images() { printf '%s:%s\n%s:%s\ngrafana/grafana:13\n' "${GC}" "\$tag" "${AG}" "\$tag"; }
case " \$* " in
  *" config --images "*) images; exit 0 ;;
  *" config --services "*) cat "\$C/services"; exit 0 ;;
  *" compose "*" pull "*)
    images | while read -r i; do
      [[ -f "\$C/pull-skip" ]] && grep -qF "\$(cat "\$C/pull-skip")" <<<"\$i" && continue
      grep -qxF "\$i" "\$C/images.local" || echo "\$i" >>"\$C/images.local"
    done; exit 0 ;;
  *" compose "*" ps "*)
    while read -r s; do
      if [[ -f "\$C/unhealthy-tag" && "\$tag" == "\$(cat "\$C/unhealthy-tag")" && "\$s" == game-coordinator ]]; then
        echo "\$s|exited|"
      else echo "\$s|running|healthy"; fi
    done <"\$C/services"; exit 0 ;;
  " image inspect "*) grep -qxF "\$3" "\$C/images.local"; exit \$? ;;
  " image ls "*) cat "\$C/images.local"; exit 0 ;;
  " image rm "*) grep -vxF "\$3" "\$C/images.local" >"\$C/images.new" || true; mv "\$C/images.new" "\$C/images.local"; exit 0 ;;
esac
exit 0
STUB
  cat >"${CASE}/bin/curl" <<STUB
#!/usr/bin/env bash
echo "curl \$*" >>"${CASE}/calls.log"
printf '%s' "\$(cat "${CASE}/http_code")"
STUB
  cat >"${CASE}/bin/systemctl" <<STUB
#!/usr/bin/env bash
echo "systemctl \$*" >>"${CASE}/calls.log"
[[ "\$1" == show ]] && { cat "${CASE}/unit-env"; exit 0; }
[[ "\$1" == restart ]] && exit "\$(cat "${CASE}/restart_rc")"
exit 0
STUB
  printf '#!/usr/bin/env bash\necho "sudo $*" >>"%s/calls.log"\nexec "$@"\n' "${CASE}" >"${CASE}/bin/sudo"
  printf '#!/usr/bin/env bash\n[[ "$1" == -u ]] && { echo 1000; exit 0; }\nexec /usr/bin/id "$@"\n' >"${CASE}/bin/id"
  cat >"${CASE}/bin/git" <<STUB
#!/usr/bin/env bash
echo "git \$*" >>"${CASE}/calls.log"
exec "${REAL_GIT}" -c commit.gpgsign=false "\$@"
STUB
  chmod +x "${CASE}/bin/"*
}

# upgrade <args...>: runs the checkout's own command; returns its exit code
upgrade() {
  env PATH="${CASE}/bin:${PATH}" TMPDIR="${CASE}/tmp" JOUSTMANIA_HEALTH_TIMEOUT=1 JOUSTMANIA_HEALTH_POLL=1 \
    bash "${REPO}/scripts/setup/joustmania-upgrade.sh" "$@" >"${CASE}/out.log" 2>&1
}
head_of() { g -C "${REPO}" rev-parse HEAD; }
rel() { sed -n "s/^$1=//p" "${REPO}/.joustmania-release" 2>/dev/null; }
worktrees() { g -C "${REPO}" worktree list --porcelain | grep -c '^worktree ' || true; }
nothing_changed() { # $1 = label, $2 = expected HEAD
  [[ "$(head_of)" == "$2" ]] || fail "$1: HEAD moved"
  grep -q 'git .*checkout' "${CASE}/calls.log" && fail "$1: git checkout was called"
  grep -q 'restart' "${CASE}/calls.log" && fail "$1: the unit was restarted"
  [[ -e "${REPO}/.joustmania-release" ]] && fail "$1: .joustmania-release was written"
  grep -q 'image rm' "${CASE}/calls.log" && fail "$1: an image was removed"
  [[ "$(cat "${REPO}/.env.local")" == $'IMAGE_TAG=stale-local\nJEV_API_KEY=dummy-secret-2027' ]] || fail "$1: .env.local changed"
  return 0
}

# --- 1. refused targets (BR-003): latest, main, short sha, branch, below the floor
for bad in latest main "${C_V11:0:12}" feature/x joustmania-v0.1.0 0.9.0 "${C_OLD}"; do
  make_case "refuse-${bad//\//-}"
  if upgrade "${bad}"; then fail "refuse ${bad}: accepted"; fi
  grep -qi 'refusing' "${CASE}/out.log" || fail "refuse ${bad}: no refusal message: $(tail -1 "${CASE}/out.log")"
  nothing_changed "refuse ${bad}" "${C_V1}"
  grep -q 'compose.* pull' "${CASE}/calls.log" && fail "refuse ${bad}: images were pulled"
done

# --- 2. registry down or a captive portal: no fetch, nothing changed ---------
for code in 000 302 503; do
  make_case "registry-${code}"
  echo "${code}" >"${CASE}/http_code"
  if upgrade joustmania-v1.1.0; then fail "registry ${code}: upgrade succeeded"; fi
  grep -q 'unreachable' "${CASE}/out.log" || fail "registry ${code}: no 'unreachable' message"
  grep -q 'git fetch' "${CASE}/calls.log" && fail "registry ${code}: fetched although the registry is down"
  grep -q -- "-w %{http_code}" "${CASE}/calls.log" || fail "registry ${code}: probe does not read the HTTP code"
  nothing_changed "registry ${code}" "${C_V1}"
done
make_case registry-401
echo 401 >"${CASE}/http_code"
upgrade joustmania-v1.1.0 || fail "registry 401: upgrade refused ($(tail -1 "${CASE}/out.log"))"

# --- 3. prepare pull leaves an image missing: listed, nothing changed --------
make_case pull-missing
echo "${AG}" >"${CASE}/pull-skip"
if upgrade joustmania-v1.1.0; then fail "pull-missing: upgrade succeeded"; fi
grep -q "missing after the pull: ${AG}:1.1.0" "${CASE}/out.log" || fail "pull-missing: missing image not listed: $(tail -1 "${CASE}/out.log")"
nothing_changed pull-missing "${C_V1}"
[[ "$(worktrees)" == 1 ]] || fail "pull-missing: the temporary worktree was left behind"

# --- 4. tag accepted: switch, restart, record, prune ------------------------
make_case tag "${C_V1}"
g -C "${REPO}" worktree add -q --detach "${CASE}/stale" "${C_V1}"
rm -rf "${CASE}/stale" # a run killed before its trap: stale registration
upgrade joustmania-v1.1.0 || fail "tag: upgrade failed: $(tail -3 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V11}" ]] || fail "tag: HEAD is not joustmania-v1.1.0"
g -C "${REPO}" symbolic-ref -q HEAD >/dev/null && fail "tag: HEAD is on a branch, not detached"
[[ "$(rel current_ref)" == "${C_V11}" && "$(rel current_tag)" == 1.1.0 ]] || fail "tag: current not recorded"
[[ "$(rel previous_ref)" == "${C_V1}" && "$(rel previous_tag)" == 1.0.0 ]] || fail "tag: previous not recorded"
grep -qx "sudo systemctl restart joustmania" "${CASE}/calls.log" || fail "tag: unit not restarted via sudo systemctl"
grep -q "compose.* pull | tag=1.1.0" "${CASE}/calls.log" || fail "tag: target images not pulled with tag 1.1.0"
[[ "$(cat "${REPO}/.env.local")" == $'IMAGE_TAG=stale-local\nJEV_API_KEY=dummy-secret-2027' ]] || fail "tag: .env.local was rewritten"
grep -q 'dummy-secret-2027' "${CASE}/out.log" && fail "tag: a secret was printed"
grep -q 'WARN: .env.local sets IMAGE_TAG' "${CASE}/out.log" || fail "tag: no WARN for IMAGE_TAG in .env.local"
[[ "$(worktrees)" == 1 ]] || fail "tag: a worktree is left (stale one not pruned, or the temporary one kept)"
prune_line="$(grep -n '^git worktree prune$' "${CASE}/calls.log" | head -n 1 | cut -d: -f1 || true)"
add_line="$(grep -n '^git worktree add' "${CASE}/calls.log" | head -n 1 | cut -d: -f1 || true)"
[[ -n "${prune_line}" && -n "${add_line}" && "${prune_line}" -lt "${add_line}" ]] \
  || fail "tag: git worktree prune does not run before the temporary worktree is added"
for keep in "${GC}:1.1.0" "${AG}:1.1.0" "${GC}:1.0.0" "${AG}:1.0.0" grafana/grafana:13 busybox:latest; do
  grep -qxF "${keep}" "${CASE}/images.local" || fail "tag: prune removed ${keep}"
done
for gone in "${GC}:0.9.0" grafana/grafana:12; do
  grep -qxF "${gone}" "${CASE}/images.local" && fail "tag: superseded ${gone} not pruned"
done

# --- 5. --rollback, offline: no registry, no fetch --------------------------
echo 000 >"${CASE}/http_code"
: >"${CASE}/calls.log"
upgrade --rollback || fail "rollback: failed: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "rollback: HEAD is not back at 1.0.0"
grep -qE '^curl|git fetch' "${CASE}/calls.log" && fail "rollback: touched the network"
grep -qx "sudo systemctl restart joustmania" "${CASE}/calls.log" || fail "rollback: unit not restarted"
[[ "$(rel current_ref)" == "${C_V1}" && "$(rel previous_ref)" == "${C_V11}" ]] || fail "rollback: record not swapped"
make_case rollback-norecord "${C_V1}"
if upgrade --rollback; then fail "rollback-norecord: succeeded without a record"; fi
nothing_changed rollback-norecord "${C_V1}"

# --- 6. sha accepted from a branch checkout (the one-time migration) --------
make_case sha
printf 'COMPOSE_PROFILES=python-hid JOUSTMANIA_UNIT_REV=2\n' >"${CASE}/unit-env"
upgrade "${C_MAIN}" || fail "sha: upgrade failed: $(tail -3 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_MAIN}" ]] || fail "sha: HEAD is not the sha"
[[ "$(rel current_tag)" == "${C_MAIN}" ]] || fail "sha: image tag is not the full sha: $(rel current_tag)"
[[ "$(rel previous_ref)" == "${C_V1}" ]] || fail "sha: previous is not the branch commit"
grep -q "compose.* pull | tag=${C_MAIN} profiles=python-hid" "${CASE}/calls.log" \
  || fail "sha: pull did not use the sha tag and the unit's COMPOSE_PROFILES"

# --- 7. not healthy: automatic rollback; --no-rollback leaves it ------------
make_case unhealthy "${C_V1}"
echo 1.1.0 >"${CASE}/unhealthy-tag"
if upgrade joustmania-v1.1.0; then fail "unhealthy: reported success"; fi
[[ "$(head_of)" == "${C_V1}" ]] || fail "unhealthy: not rolled back to 1.0.0"
[[ "$(grep -c '^sudo systemctl restart joustmania' "${CASE}/calls.log")" -ge 2 ]] || fail "unhealthy: previous version not restarted"
grep -q 'rolled back' "${CASE}/out.log" || fail "unhealthy: rollback not reported"
[[ "$(rel current_ref)" == "${C_V1}" ]] || fail "unhealthy: record does not name 1.0.0 as current"
grep -q 'image rm' "${CASE}/calls.log" && fail "unhealthy: images pruned after a failed upgrade"

make_case no-rollback "${C_V1}"
echo 1.1.0 >"${CASE}/unhealthy-tag"
if upgrade --no-rollback joustmania-v1.1.0; then fail "no-rollback: reported success"; fi
[[ "$(head_of)" == "${C_V11}" ]] || fail "no-rollback: the failed version was not left in place"
[[ "$(grep -c '^sudo systemctl restart joustmania' "${CASE}/calls.log")" == 1 ]] || fail "no-rollback: restarted more than once"

make_case restart-fails "${C_V1}"
echo 1 >"${CASE}/restart_rc"
if upgrade --no-rollback joustmania-v1.1.0; then fail "restart-fails: a failed unit start counted as healthy"; fi

# --- 8. prune fails closed without a complete record -------------------------
make_case prune-closed "${C_V1}"
(
  cd "${REPO}"
  PATH="${CASE}/bin:${PATH}"
  # shellcheck source=/dev/null
  source scripts/setup/joustmania-upgrade.sh
  prune_images
  printf 'current_ref=x\ncurrent_images=%s:1.0.0\n' "${GC}" >.joustmania-release
  prune_images
) >"${CASE}/out.log" 2>&1 || fail "prune-closed: prune_images failed"
grep -q 'image rm' "${CASE}/calls.log" && fail "prune-closed: removed images without a complete record"

# --- 9. the pin: IMAGE_TAG derived from HEAD over .env.local (BR-003, A4) ----
pin_tag() { env PATH="${CASE}/bin:${PATH}" "${REPO}/scripts/setup/joustmania-compose.sh" config --images | sed -n "s|^${GC}:||p"; }
make_case pin "${C_V11}"
[[ "$(pin_tag)" == 1.1.0 ]] || fail "pin: release tag checkout does not give 1.1.0: $(pin_tag)"
g -C "${REPO}" checkout -q --detach "${C_MAIN}"
[[ "$(pin_tag)" == "${C_MAIN}" ]] || fail "pin: untagged commit does not give the sha: $(pin_tag)"
g -C "${REPO}" checkout -q main
[[ "$(pin_tag)" == stale-local ]] || fail "pin: a branch checkout must not derive a tag: $(pin_tag)"
g -C "${REPO}" checkout -q --detach "${C_V11}"
mkdir -p "${CASE}/asound"
printf '#!/usr/bin/env bash\nexit 0\n' >"${CASE}/bin/amixer"
chmod +x "${CASE}/bin/amixer"
env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_DOCKER_WAIT_S=1 JOUSTMANIA_ASOUND_DIR="${CASE}/asound" \
  JOUSTMANIA_STATUS_DIR="${CASE}/status" JOUSTMANIA_UNIT_REV=2 \
  bash "${REPO}/scripts/setup/joustmania-start.sh" >"${CASE}/start.log" 2>&1 || fail "pin: start script failed"
grep -q 'IMAGE_TAG 1.1.0' "${CASE}/start.log" || fail "pin: start script does not run the derived tag"
grep -q 'WARN: .env.local sets IMAGE_TAG' "${CASE}/start.log" || fail "pin: start script does not WARN about IMAGE_TAG in .env.local"

if ((fails > 0)); then
  echo "test-joustmania-upgrade: ${fails} failure(s)"
  exit 1
fi
echo "test-joustmania-upgrade: OK"
