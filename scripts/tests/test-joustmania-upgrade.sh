#!/usr/bin/env bash
# The Pi upgrade command and the pin (UC-060 A2, A3, A4, BR-003; FR-182, C-040; #2027 PR 3).
#
# scripts/setup/joustmania-upgrade.sh runs in a throwaway clone of a throwaway
# repository (real git with a local bare origin, wrapped by a logging shim) and
# against stub docker, curl, systemctl, sudo and id. Nothing reaches the network,
# the real docker stack or systemd.
#  - release tag and exact sha accepted; latest, main, short sha refused; below
#    MIN_PINNABLE (version and commit floor) refused; nothing changes on a refusal;
#    the shipped MIN_PINNABLE (0.8.0) is inclusive: v0.8.0 accepted, v0.7.0 refused;
#    with MIN_PINNABLE empty the commit floor and the script check decide
#  - registry down (no 200/401) -> no fetch, no checkout, no restart
#  - prepare pull missing an image -> listed, nothing changed, temp worktree removed
#  - healthy -> switch, restart, record, prune (only superseded tags of the
#    joustmania repositories; third-party images survive); fail-closed prune
#    without a record; a failed or empty `config --services` is not healthy
#  - the version already checked out: images missing on the Pi are pulled and the
#    unit restarted (the recovery the start names, BR-001); none missing: no-op
#  - not healthy -> automatic rollback; --no-rollback leaves it; --rollback offline,
#    HEAD-relative, with an image precheck before and after the switch
#  - a version that ran from a branch (the migration) is restored on its branch,
#    never by moving a branch that moved on
#  - cut-off runs: a rollback cut off after its checkout finishes on its target
#    (never toggles back); an upgrade cut off after its checkout is flagged by
#    --status and repaired by re-running it; failed checkouts change nothing and
#    the record is written only after the checkout
#  - guards: a rollback cut off during its restart keeps rollback_to (the rerun
#    finishes on the target); re-running an already repaired upgrade changes
#    nothing; a rollback and then an upgrade both cut off: the stale marker is
#    dropped and the re-run repairs the record (previous = the rollback target)
#  - the start limit (BR-006): every restart runs reset-failed first, so a 4th
#    and 5th restart inside the window start; a start systemd still refuses is
#    retried once, then reported as refused (never unhealthy, never rolled back);
#    --status shows start-limit-hit with the recovery command
#  - a stale worktree registration is pruned; COMPOSE_PROFILES from the unit honoured
#  - joustmania-compose.sh derives IMAGE_TAG from a pinned HEAD over .env.local
#    (X.Y.Z of the release tag, else the sha; nothing on a branch), with a stderr
#    note when it overrides a shell IMAGE_TAG; the start script WARNs about an
#    IMAGE_TAG in .env.local
# Runs as part of `make lint`, or directly: ./scripts/tests/test-joustmania-upgrade.sh
# UC: UC-060 main step 2 A2 A3 A4 BR-003 BR-006
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
unset COMPOSE DOCKER COMPOSE_PROJECT_NAME COMPOSE_PROFILES OTEL_COLLECTOR_TAG IMAGE_TAG \
  JOUSTMANIA_UNIT_REV RUNTIME_DIRECTORY GIT_DIR GIT_WORK_TREE || true
# A test that reaches for the real docker fails instead of using it
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
  sed -e "s/^MIN_PINNABLE_COMMIT=.*/MIN_PINNABLE_COMMIT=\"$1\"/" -e 's/^MIN_PINNABLE=.*/MIN_PINNABLE="1.0.0"/' \
    "${ROOT}/scripts/setup/joustmania-upgrade.sh" >"${SRC}/scripts/setup/joustmania-upgrade.sh"
  chmod +x "${SRC}/scripts/setup/"*.sh
  printf '.env.local\n.joustmania-release\n.joustmania-release.tmp\n' >"${SRC}/.gitignore"
  : >"${SRC}/docker-compose.yml"
}
C_OLD="$(commit old 0.1.0)"
g -C "${SRC}" tag joustmania-v0.1.0
g -C "${SRC}" tag joustmania-v0.7.0
C_FLOOR="$(commit floor 0.1.0)"
add_scripts "${C_FLOOR}"
C_08="$(commit v0.8.0 0.8.0)"
g -C "${SRC}" tag joustmania-v0.8.0
C_09="$(commit v0.9.0 0.9.0)"
g -C "${SRC}" tag joustmania-v0.9.0
C_V1="$(commit v1.0.0 1.0.0)"
g -C "${SRC}" tag joustmania-v1.0.0
C_V11="$(commit v1.1.0 1.1.0)"
g -C "${SRC}" tag joustmania-v1.1.0
C_MAIN="$(commit main-later 1.1.0)"
# A side branch off the pre-floor commit that has the scripts but not the floor
g -C "${SRC}" checkout -q -b side "${C_OLD}"
add_scripts "${C_FLOOR}"
C_SIDE="$(commit side 0.1.0)"
# A branch off the floor commit with the scripts but without the joustmania-v0.8.0 tag
# (the stage Pi's pre-tag pin: it contains #2040, not the tag)
g -C "${SRC}" checkout -q -b pre08 "${C_FLOOR}"
add_scripts "${C_FLOOR}"
C_PRE="$(commit pre08 0.1.0)"
g -C "${SRC}" checkout -q main
ORIGIN="${WORK}/origin.git"
g clone -q --bare "${SRC}" "${ORIGIN}"
ORIGIN_NOTAG="${WORK}/origin-notag.git" # the same origin before the joustmania-v0.8.0 cut
g clone -q --bare "${ORIGIN}" "${ORIGIN_NOTAG}"
g -C "${ORIGIN_NOTAG}" tag -d joustmania-v0.8.0 >/dev/null
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
  *" config --images "*)
    # kill-on-images: power loss / SIGHUP of the command while it checks images
    if [[ -f "\$C/kill-on-images" ]]; then
      rm -f "\$C/kill-on-images"
      for _ in \$(seq 50); do [[ -s "\$C/upgrade.pid" ]] && break; sleep 0.1; done
      kill -9 "\$(cat "\$C/upgrade.pid")"
      exit 1
    fi
    images; exit 0 ;;
  *" config --services "*) [[ -f "\$C/services-fail" ]] && exit 1; cat "\$C/services"; exit 0 ;;
  *" compose "*" config "*)
    printf 'services:\n'; while read -r s; do printf '  %s:\n    image: %s:%s\n' "\$s" "${GC}" "\$tag"; done <"\$C/services"
    exit 0 ;;
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
  # systemctl models the unit's start limit (StartLimitBurst=3 in the window,
  # BR-006): every start counts, the 4th is refused with start-limit-hit until
  # reset-failed (a no-op while the file reset-noop exists) zeroes the count
  echo 0 >"${CASE}/starts"
  echo success >"${CASE}/result"
  cat >"${CASE}/bin/systemctl" <<STUB
#!/usr/bin/env bash
C="${CASE}"
echo "systemctl \$*" >>"\$C/calls.log"
case "\$1" in
  show)
    if [[ " \$* " == *" Result "* ]]; then cat "\$C/result"; else cat "\$C/unit-env"; fi
    exit 0 ;;
  reset-failed)
    [[ -f "\$C/reset-noop" ]] || { echo 0 >"\$C/starts"; echo success >"\$C/result"; }
    exit 0 ;;
  restart | start)
    # kill-on-restart: power loss / SIGHUP of the command during the restart
    if [[ -f "\$C/kill-on-restart" ]]; then
      rm -f "\$C/kill-on-restart"
      for _ in \$(seq 50); do [[ -s "\$C/upgrade.pid" ]] && break; sleep 0.1; done
      kill -9 "\$(cat "\$C/upgrade.pid")"
      exit 1
    fi
    n="\$(cat "\$C/starts")"
    if ((n >= 3)); then
      echo start-limit-hit >"\$C/result"
      echo "Job for joustmania.service failed. See 'systemctl status joustmania.service'." >&2
      exit 1
    fi
    echo \$((n + 1)) >"\$C/starts"
    rc="\$(cat "\$C/restart_rc")"
    # restart-fail-once: only the next start fails (ExecStart exits non-zero)
    [[ -f "\$C/restart-fail-once" ]] && { rm -f "\$C/restart-fail-once"; rc=1; }
    if [[ "\$rc" == 0 ]]; then echo success >"\$C/result"; else echo exit-code >"\$C/result"; fi
    exit "\$rc" ;;
esac
exit 0
STUB
  printf '#!/usr/bin/env bash\necho "sudo $*" >>"%s/calls.log"\nexec "$@"\n' "${CASE}" >"${CASE}/bin/sudo"
  printf '#!/usr/bin/env bash\n[[ "$1" == -u ]] && { echo 1000; exit 0; }\nexec /usr/bin/id "$@"\n' >"${CASE}/bin/id"
  cat >"${CASE}/bin/git" <<STUB
#!/usr/bin/env bash
echo "git \$*" >>"${CASE}/calls.log"
if [[ -f "${CASE}/checkout-fail" && " \$* " == *" checkout "* ]]; then echo "stub: checkout refused" >&2; exit 1; fi
exec "${REAL_GIT}" -c commit.gpgsign=false "\$@"
STUB
  chmod +x "${CASE}/bin/"*
}

# upgrade <args...>: runs the checkout's own command; returns its exit code
upgrade() {
  env PATH="${CASE}/bin:${PATH}" TMPDIR="${CASE}/tmp" JOUSTMANIA_HEALTH_TIMEOUT=1 JOUSTMANIA_HEALTH_POLL=1 \
    bash "${REPO}/scripts/setup/joustmania-upgrade.sh" "$@" >"${CASE}/out.log" 2>&1
}
# upgrade_killed <args...>: like upgrade, for a run the docker stub kills (kill-on-images)
upgrade_killed() {
  rm -f "${CASE}/upgrade.pid"
  env PATH="${CASE}/bin:${PATH}" TMPDIR="${CASE}/tmp" JOUSTMANIA_HEALTH_TIMEOUT=1 JOUSTMANIA_HEALTH_POLL=1 \
    bash "${REPO}/scripts/setup/joustmania-upgrade.sh" "$@" >"${CASE}/out.log" 2>&1 &
  echo "$!" >"${CASE}/upgrade.pid"
  { wait "$!"; } 2>/dev/null || true
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

# --- 1b. the floor as shipped: MIN_PINNABLE 0.8.0 (inclusive); blanked -> commit floor + script check
# The checkout's command with only the commit floor swapped in (untracked copy);
# SHIPPED_MIN="" blanks MIN_PINNABLE to exercise the commit floor alone.
SHIPPED_MIN=0.8.0
shipped() {
  sed -e "s/^MIN_PINNABLE_COMMIT=.*/MIN_PINNABLE_COMMIT=\"${C_FLOOR}\"/" -e "s/^MIN_PINNABLE=.*/MIN_PINNABLE=\"${SHIPPED_MIN}\"/" \
    "${ROOT}/scripts/setup/joustmania-upgrade.sh" >"${REPO}/scripts/setup/joustmania-upgrade-shipped.sh"
  grep -q '^MIN_PINNABLE="0.8.0"$' "${ROOT}/scripts/setup/joustmania-upgrade.sh" || fail "shipped: MIN_PINNABLE is not 0.8.0"
  env PATH="${CASE}/bin:${PATH}" TMPDIR="${CASE}/tmp" JOUSTMANIA_HEALTH_TIMEOUT=1 JOUSTMANIA_HEALTH_POLL=1 \
    bash "${REPO}/scripts/setup/joustmania-upgrade-shipped.sh" "$@" >"${CASE}/out.log" 2>&1
}
for bad in "${C_FLOOR}:no scripts/setup/joustmania-upgrade.sh" "${C_SIDE}:older than the pinnable floor"; do
  make_case "floor-${bad:0:8}" "${C_V1}"
  SHIPPED_MIN=""
  if shipped "${bad%%:*}"; then fail "floor ${bad}: accepted with MIN_PINNABLE empty"; fi
  SHIPPED_MIN=0.8.0
  grep -q "refusing.*${bad#*:}" "${CASE}/out.log" || fail "floor ${bad}: wrong or no refusal: $(tail -1 "${CASE}/out.log")"
  grep -q 'git worktree add' "${CASE}/calls.log" && fail "floor ${bad}: prepared the target"
  nothing_changed "floor ${bad}" "${C_V1}"
done
make_case floor-accept-08 "${C_V1}"
shipped joustmania-v0.8.0 || fail "floor: 0.8.0 (MIN_PINNABLE itself) refused: $(tail -1 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_08}" ]] || fail "floor: HEAD is not 0.8.0"
make_case floor-refuse-07 "${C_V1}"
if shipped joustmania-v0.7.0; then fail "floor: 0.7.0 accepted below MIN_PINNABLE 0.8.0"; fi
grep -q 'refusing.*older than MIN_PINNABLE 0.8.0' "${CASE}/out.log" || fail "floor: 0.7.0 wrong refusal: $(tail -1 "${CASE}/out.log")"
nothing_changed "floor 0.7.0" "${C_V1}"
make_case floor-accept-09 "${C_V1}"
shipped joustmania-v0.9.0 || fail "floor: 0.9.0 refused with MIN_PINNABLE 0.8.0: $(tail -1 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_09}" ]] || fail "floor: HEAD is not 0.9.0"
make_case floor-accept-sha "${C_V1}"
shipped "${C_09}" || fail "floor: the 0.9.0 sha refused with MIN_PINNABLE 0.8.0: $(tail -1 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_09}" ]] || fail "floor: HEAD is not the 0.9.0 sha"

# --- 1c. the tag check (BR-003), and the way back to a pre-tag commit is --rollback
make_case floor-pre-tag "${C_08}"
if shipped "${C_PRE}"; then fail "pre-tag: an explicit upgrade to a commit without joustmania-v0.8.0 was accepted"; fi
grep -q 'refusing.*does not contain MIN_PINNABLE joustmania-v0.8.0' "${CASE}/out.log" || fail "pre-tag: wrong or no refusal: $(tail -1 "${CASE}/out.log")"
nothing_changed "pre-tag" "${C_08}"
make_case floor-pre-tag-rollback "${C_PRE}"
printf '%s:%s\n%s:%s\n' "${GC}" "${C_PRE}" "${AG}" "${C_PRE}" >>"${CASE}/images.local" # the pre-tag pin ran from its sha tag
shipped joustmania-v0.8.0 || fail "pre-tag: 0.8.0 from the pre-tag pin refused: $(tail -1 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_08}" ]] || fail "pre-tag: HEAD is not 0.8.0"
shipped --rollback || fail "pre-tag: --rollback to the pre-tag commit refused: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_PRE}" ]] || fail "pre-tag: --rollback did not land on the pre-tag commit"
make_case floor-no-tag "${C_08}"
g -C "${REPO}" remote set-url origin "${ORIGIN_NOTAG}"
g -C "${REPO}" tag -d joustmania-v0.8.0 >/dev/null
shipped "${C_PRE}" || fail "no-tag: the pre-tag sha refused while the tag is absent: $(tail -1 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_PRE}" ]] || fail "no-tag: HEAD is not the pre-tag sha"
make_case floor-no-tag-07 "${C_PRE}"
g -C "${REPO}" remote set-url origin "${ORIGIN_NOTAG}"
g -C "${REPO}" tag -d joustmania-v0.8.0 >/dev/null
if shipped joustmania-v0.7.0; then fail "no-tag: 0.7.0 accepted"; fi
grep -q 'refusing.*older than MIN_PINNABLE 0.8.0' "${CASE}/out.log" || fail "no-tag: 0.7.0 wrong refusal: $(tail -1 "${CASE}/out.log")"
nothing_changed "no-tag 0.7.0" "${C_PRE}"

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
for keep in "${GC}:1.1.0" "${AG}:1.1.0" "${GC}:1.0.0" "${AG}:1.0.0" grafana/grafana:13 grafana/grafana:12 busybox:latest; do
  grep -qxF "${keep}" "${CASE}/images.local" || fail "tag: prune removed ${keep}"
done
for gone in "${GC}:0.9.0"; do
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

# --rollback is HEAD-relative: a record swapped by an interrupted rollback (HEAD
# still at 1.1.0) still moves HEAD to 1.0.0, never restarts the running version
make_case rollback-interrupted "${C_V1}"
upgrade joustmania-v1.1.0 || fail "rollback-interrupted: upgrade failed: $(tail -2 "${CASE}/out.log")"
sed -i -e 's/^current_/x_/' -e 's/^previous_/current_/' -e 's/^x_/previous_/' "${REPO}/.joustmania-release"
[[ "$(rel current_ref)" == "${C_V1}" && "$(head_of)" == "${C_V11}" ]] || fail "rollback-interrupted: setup"
upgrade --rollback || fail "rollback-interrupted: failed: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "rollback-interrupted: HEAD is not 1.0.0 (restarted the running version)"
[[ "$(rel current_ref)" == "${C_V1}" && "$(rel previous_ref)" == "${C_V11}" ]] || fail "rollback-interrupted: record does not name 1.0.0 as current"
# HEAD matches neither recorded version: refused, nothing changed
g -C "${REPO}" checkout -q --detach "${C_09}"
: >"${CASE}/calls.log"
cp "${REPO}/.joustmania-release" "${CASE}/release.before"
if upgrade --rollback; then fail "rollback-neither: succeeded"; fi
[[ "$(head_of)" == "${C_09}" ]] || fail "rollback-neither: HEAD moved"
grep -q restart "${CASE}/calls.log" && fail "rollback-neither: the unit was restarted"
cmp -s "${CASE}/release.before" "${REPO}/.joustmania-release" || fail "rollback-neither: record changed"

# --rollback precheck: a previous image missing -> refused, no checkout, no restart
make_case rollback-precheck "${C_V1}"
upgrade joustmania-v1.1.0 || fail "rollback-precheck: upgrade failed: $(tail -2 "${CASE}/out.log")"
grep -vxF "${AG}:1.0.0" "${CASE}/images.local" >"${CASE}/images.new" || true
mv "${CASE}/images.new" "${CASE}/images.local"
cp "${REPO}/.joustmania-release" "${CASE}/release.before"
: >"${CASE}/calls.log"
if upgrade --rollback; then fail "rollback-precheck: succeeded with a previous image missing"; fi
grep -q "not on the Pi: ${AG}:1.0.0" "${CASE}/out.log" || fail "rollback-precheck: missing image not listed: $(tail -1 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V11}" ]] || fail "rollback-precheck: HEAD moved"
grep -q 'git .*checkout' "${CASE}/calls.log" && fail "rollback-precheck: git checkout was called"
grep -q restart "${CASE}/calls.log" && fail "rollback-precheck: the unit was restarted"
cmp -s "${CASE}/release.before" "${REPO}/.joustmania-release" || fail "rollback-precheck: record changed"

# --rollback cut off after its checkout, while it checks the images (ssh SIGHUP,
# power loss): HEAD is at 1.0.0 but nothing restarted. The next --rollback
# finishes on 1.0.0 and never toggles back to the bad 1.1.0
make_case rollback-cutoff "${C_V1}"
upgrade joustmania-v1.1.0 || fail "rollback-cutoff: upgrade failed: $(tail -2 "${CASE}/out.log")"
touch "${CASE}/kill-on-images"
: >"${CASE}/calls.log"
upgrade_killed --rollback
[[ -f "${CASE}/kill-on-images" ]] && fail "rollback-cutoff: setup: the run was not cut off"
[[ "$(head_of)" == "${C_V1}" ]] || fail "rollback-cutoff: setup: the cut-off run had not checked out 1.0.0"
grep -q restart "${CASE}/calls.log" && fail "rollback-cutoff: setup: restarted before the cut"
upgrade --status || fail "rollback-cutoff: --status failed"
grep -q "WARN: a rollback to ${C_V1} was cut off" "${CASE}/out.log" || fail "rollback-cutoff: --status does not warn: $(cat "${CASE}/out.log")"
: >"${CASE}/calls.log"
upgrade --rollback || fail "rollback-cutoff: rerun failed: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "rollback-cutoff: the rerun toggled back to the bad 1.1.0"
grep -qx "sudo systemctl restart joustmania" "${CASE}/calls.log" || fail "rollback-cutoff: the rerun did not restart 1.0.0"
[[ "$(rel current_ref)" == "${C_V1}" && "$(rel previous_ref)" == "${C_V11}" ]] || fail "rollback-cutoff: record does not name 1.0.0 as current"
grep -q '^rollback_to=' "${REPO}/.joustmania-release" && fail "rollback-cutoff: rollback_to left in the record"

# --rollback when the rollback checkout fails: no record swap, no restart
make_case rollback-checkout-fails "${C_V1}"
upgrade joustmania-v1.1.0 || fail "rollback-checkout-fails: upgrade failed: $(tail -2 "${CASE}/out.log")"
touch "${CASE}/checkout-fail"
cp "${REPO}/.joustmania-release" "${CASE}/release.before"
: >"${CASE}/calls.log"
if upgrade --rollback; then fail "rollback-checkout-fails: succeeded"; fi
[[ "$(head_of)" == "${C_V11}" ]] || fail "rollback-checkout-fails: HEAD moved"
grep -q restart "${CASE}/calls.log" && fail "rollback-checkout-fails: the unit was restarted"
cmp -s "${CASE}/release.before" "${REPO}/.joustmania-release" || fail "rollback-checkout-fails: record changed"
grep -q "nothing changed: ${C_V11} still runs" "${CASE}/out.log" || fail "rollback-checkout-fails: no 'nothing changed': $(tail -1 "${CASE}/out.log")"
grep -q 'state failed' "${CASE}/out.log" && fail "rollback-checkout-fails: says state failed although nothing changed"

# upgrade when its checkout fails: the record is written only after the checkout
make_case upgrade-checkout-fails "${C_V1}"
touch "${CASE}/checkout-fail"
if upgrade joustmania-v1.1.0; then fail "upgrade-checkout-fails: succeeded"; fi
[[ "$(head_of)" == "${C_V1}" ]] || fail "upgrade-checkout-fails: HEAD moved"
[[ -e "${REPO}/.joustmania-release" ]] && fail "upgrade-checkout-fails: record written before the checkout"
grep -q restart "${CASE}/calls.log" && fail "upgrade-checkout-fails: the unit was restarted"
grep -q 'nothing changed' "${CASE}/out.log" || fail "upgrade-checkout-fails: no 'nothing changed': $(tail -1 "${CASE}/out.log")"

# upgrade cut off after its checkout, before its record write: --status WARNs;
# re-running the same upgrade repairs the record so --rollback works again
make_case upgrade-cutoff "${C_V1}"
upgrade joustmania-v1.1.0 || fail "upgrade-cutoff: upgrade failed: $(tail -2 "${CASE}/out.log")"
g -C "${REPO}" checkout -q --detach "${C_MAIN}"
printf '%s:%s\n%s:%s\n' "${GC}" "${C_MAIN}" "${AG}" "${C_MAIN}" >>"${CASE}/images.local" # pulled before the cut
upgrade --status || fail "upgrade-cutoff: --status failed"
grep -q "WARN: HEAD ${C_MAIN} is neither recorded version" "${CASE}/out.log" || fail "upgrade-cutoff: --status does not warn"
: >"${CASE}/calls.log"
upgrade "${C_MAIN}" || fail "upgrade-cutoff: rerun failed: $(tail -2 "${CASE}/out.log")"
grep -q 'repaired the stale' "${CASE}/out.log" || fail "upgrade-cutoff: record not repaired: $(tail -1 "${CASE}/out.log")"
[[ "$(rel current_ref)" == "${C_MAIN}" && "$(rel current_tag)" == "${C_MAIN}" ]] || fail "upgrade-cutoff: current not repaired"
[[ "$(rel previous_ref)" == "${C_V11}" && "$(rel previous_tag)" == 1.1.0 ]] || fail "upgrade-cutoff: previous is not the recorded current"
grep -q restart "${CASE}/calls.log" && fail "upgrade-cutoff: the repair restarted the unit"
upgrade --status || fail "upgrade-cutoff: --status failed after the repair"
grep -q 'neither recorded version' "${CASE}/out.log" && fail "upgrade-cutoff: --status still warns after the repair"
# running the same upgrade again: the record is current, so nothing changes
cp "${REPO}/.joustmania-release" "${CASE}/release.before"
: >"${CASE}/calls.log"
upgrade "${C_MAIN}" || fail "upgrade-cutoff: second rerun failed: $(tail -2 "${CASE}/out.log")"
cmp -s "${CASE}/release.before" "${REPO}/.joustmania-release" || fail "upgrade-cutoff: the second rerun changed the record"
grep -q restart "${CASE}/calls.log" && fail "upgrade-cutoff: the second rerun restarted the unit"
upgrade --rollback || fail "upgrade-cutoff: --rollback after the repair failed: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V11}" ]] || fail "upgrade-cutoff: --rollback did not return to 1.1.0"
# --rollback cut off during the restart (after the second record write): rollback_to
# is still there, so the rerun finishes on 1.0.0 and does not toggle back
make_case rollback-cutoff-restart "${C_V1}"
upgrade joustmania-v1.1.0 || fail "rollback-cutoff-restart: upgrade failed: $(tail -2 "${CASE}/out.log")"
touch "${CASE}/kill-on-restart"
upgrade_killed --rollback
[[ -f "${CASE}/kill-on-restart" ]] && fail "rollback-cutoff-restart: setup: the run was not cut off"
[[ "$(head_of)" == "${C_V1}" ]] || fail "rollback-cutoff-restart: setup: HEAD is not 1.0.0"
[[ "$(rel rollback_to)" == "${C_V1}" ]] || fail "rollback-cutoff-restart: rollback_to dropped before the restart was judged"
: >"${CASE}/calls.log"
upgrade --rollback || fail "rollback-cutoff-restart: rerun failed: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "rollback-cutoff-restart: the rerun toggled back to 1.1.0"
grep -qx "sudo systemctl restart joustmania" "${CASE}/calls.log" || fail "rollback-cutoff-restart: the rerun did not restart 1.0.0"
grep -q '^rollback_to=' "${REPO}/.joustmania-release" && fail "rollback-cutoff-restart: rollback_to left after the judged restart"

# Two cut-offs in a row: a rollback cut off (marker left), then an upgrade cut off
# after its checkout. HEAD is neither recorded version: --status WARNs once (no
# "rollback cut off"), the same upgrade repairs the record (previous = the
# rollback target 1.0.0), clears the marker and restarts nothing, --rollback works
make_case double-cutoff "${C_V1}"
upgrade joustmania-v1.1.0 || fail "double-cutoff: upgrade failed: $(tail -2 "${CASE}/out.log")"
touch "${CASE}/kill-on-images"
upgrade_killed --rollback
[[ "$(rel rollback_to)" == "${C_V1}" && "$(head_of)" == "${C_V1}" ]] || fail "double-cutoff: setup: rollback not cut off with its marker"
g -C "${REPO}" checkout -q --detach "${C_MAIN}"
printf '%s:%s\n%s:%s\n' "${GC}" "${C_MAIN}" "${AG}" "${C_MAIN}" >>"${CASE}/images.local"
upgrade --status || fail "double-cutoff: --status failed"
grep -q "neither recorded version" "${CASE}/out.log" || fail "double-cutoff: --status does not warn about HEAD"
grep -q "was cut off" "${CASE}/out.log" && fail "double-cutoff: --status also warns about a rollback cut off: $(cat "${CASE}/out.log")"
: >"${CASE}/calls.log"
upgrade "${C_MAIN}" || fail "double-cutoff: rerun failed: $(tail -2 "${CASE}/out.log")"
grep -q 'repaired the stale' "${CASE}/out.log" || fail "double-cutoff: record not repaired: $(tail -1 "${CASE}/out.log")"
[[ "$(rel current_ref)" == "${C_MAIN}" && "$(rel previous_ref)" == "${C_V1}" && "$(rel previous_tag)" == 1.0.0 ]] \
  || fail "double-cutoff: record is not current MAIN / previous 1.0.0"
grep -q '^rollback_to=' "${REPO}/.joustmania-release" && fail "double-cutoff: stale rollback_to kept"
grep -q restart "${CASE}/calls.log" && fail "double-cutoff: the repair restarted the unit"
upgrade --status || fail "double-cutoff: --status failed after the repair"
grep -qE 'neither recorded version|was cut off' "${CASE}/out.log" && fail "double-cutoff: --status still warns after the repair"
upgrade --rollback || fail "double-cutoff: --rollback after the repair failed: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "double-cutoff: --rollback did not return to 1.0.0"
# Same, but the rollback was cut off during the RESTART (2nd record write: current
# is the rollback target 1.0.0, previous the failed 1.1.0): the repair keeps 1.0.0
make_case double-cutoff-restart "${C_V1}"
upgrade joustmania-v1.1.0 || fail "double-cutoff-restart: upgrade failed: $(tail -2 "${CASE}/out.log")"
touch "${CASE}/kill-on-restart"
upgrade_killed --rollback
[[ "$(rel rollback_to)" == "${C_V1}" && "$(rel current_ref)" == "${C_V1}" && "$(head_of)" == "${C_V1}" ]] || fail "double-cutoff-restart: setup"
g -C "${REPO}" checkout -q --detach "${C_MAIN}"
printf '%s:%s\n%s:%s\n' "${GC}" "${C_MAIN}" "${AG}" "${C_MAIN}" >>"${CASE}/images.local"
upgrade "${C_MAIN}" || fail "double-cutoff-restart: rerun failed: $(tail -2 "${CASE}/out.log")"
[[ "$(rel previous_ref)" == "${C_V1}" && "$(rel previous_tag)" == 1.0.0 ]] || fail "double-cutoff-restart: previous is not 1.0.0"
upgrade --rollback || fail "double-cutoff-restart: --rollback failed: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "double-cutoff-restart: --rollback went to the failed 1.1.0"

# A live marker (HEAD is a recorded version): re-running the upgrade for HEAD must
# not touch the record, or the other version and the marker are lost
make_case live-marker "${C_V1}"
upgrade joustmania-v1.1.0 || fail "live-marker: upgrade failed: $(tail -2 "${CASE}/out.log")"
touch "${CASE}/kill-on-images"
upgrade_killed --rollback
[[ "$(rel rollback_to)" == "${C_V1}" && "$(head_of)" == "${C_V1}" ]] || fail "live-marker: setup"
upgrade "${C_V1}" || fail "live-marker: rerun failed: $(tail -2 "${CASE}/out.log")"
[[ "$(rel current_ref)" == "${C_V11}" && "$(rel previous_ref)" == "${C_V1}" && "$(rel rollback_to)" == "${C_V1}" ]] \
  || fail "live-marker: the rerun changed the record"
# The migration cut off the same way: no record, the previous branch is lost; WARN
make_case migrate-cutoff
g -C "${REPO}" checkout -q --detach "${C_MAIN}"
printf '%s:%s\n%s:%s\n' "${GC}" "${C_MAIN}" "${AG}" "${C_MAIN}" >>"${CASE}/images.local"
upgrade "${C_MAIN}" || fail "migrate-cutoff: rerun failed: $(tail -2 "${CASE}/out.log")"
grep -q 'WARN: no .joustmania-release' "${CASE}/out.log" || fail "migrate-cutoff: no WARN without a record"

# --- 5c. the version already checked out: images missing on the Pi are pulled
# and the unit restarted (the recovery joustmania-start.sh names, BR-001);
# nothing missing: nothing to do, no pull, no restart
make_case same-present "${C_V1}"
upgrade joustmania-v1.0.0 || fail "same-present: failed: $(tail -2 "${CASE}/out.log")"
grep -q 'already at joustmania-v1.0.0; nothing to do' "${CASE}/out.log" || fail "same-present: not 'nothing to do': $(tail -1 "${CASE}/out.log")"
grep -q ' pull ' "${CASE}/calls.log" && fail "same-present: pulled although nothing is missing"
grep -q restart "${CASE}/calls.log" && fail "same-present: restarted although nothing is missing"
make_case same-missing "${C_V1}"
grep -vxF "${AG}:1.0.0" "${CASE}/images.local" >"${CASE}/images.new" && mv "${CASE}/images.new" "${CASE}/images.local"
upgrade joustmania-v1.0.0 || fail "same-missing: failed: $(tail -2 "${CASE}/out.log")"
grep -q "images are missing on the Pi: ${AG}:1.0.0; pulling them" "${CASE}/out.log" || fail "same-missing: missing image not named: $(cat "${CASE}/out.log")"
grep -q "compose.* pull | tag=1.0.0" "${CASE}/calls.log" || fail "same-missing: not pulled"
grep -qxF "${AG}:1.0.0" "${CASE}/images.local" || fail "same-missing: image still missing"
grep -qx "sudo systemctl restart joustmania" "${CASE}/calls.log" || fail "same-missing: unit not restarted"
[[ "$(head_of)" == "${C_V1}" ]] || fail "same-missing: HEAD moved"
make_case same-missing-offline "${C_V1}"
grep -vxF "${AG}:1.0.0" "${CASE}/images.local" >"${CASE}/images.new" && mv "${CASE}/images.new" "${CASE}/images.local"
echo 000 >"${CASE}/http_code"
if upgrade joustmania-v1.0.0; then fail "same-missing-offline: succeeded offline"; fi
grep -q restart "${CASE}/calls.log" && fail "same-missing-offline: restarted"

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

# The health check fails closed: `config --services` failing, or no game service
make_case services-fail "${C_V1}"
touch "${CASE}/services-fail"
if upgrade --no-rollback joustmania-v1.1.0; then fail "services-fail: no service list counted as healthy"; fi
grep -q 'config --services failed' "${CASE}/out.log" || fail "services-fail: not reported"
make_case services-none "${C_V1}"
printf 'jaeger\n' >"${CASE}/services"
if upgrade --no-rollback joustmania-v1.1.0; then fail "services-none: no game service counted as healthy"; fi

# --- 7a. the start limit (BR-006, #2102) -----------------------------------
# Upgrade, --rollback, upgrade, --rollback inside the window: four operator
# restarts, each after reset-failed, so the 4th is not refused
make_case start-limit "${C_V1}"
upgrade joustmania-v1.1.0 || fail "start-limit: 1st upgrade failed: $(tail -2 "${CASE}/out.log")"
upgrade --rollback || fail "start-limit: 1st --rollback failed: $(tail -2 "${CASE}/out.log")"
upgrade joustmania-v1.1.0 || fail "start-limit: 2nd upgrade failed: $(tail -2 "${CASE}/out.log")"
upgrade --rollback || fail "start-limit: 4th restart (--rollback) refused: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "start-limit: HEAD is not 1.0.0 after the 4th restart"
[[ "$(grep -c '^sudo systemctl restart joustmania' "${CASE}/calls.log")" == 4 ]] || fail "start-limit: not 4 restarts"
upgrade joustmania-v1.1.0 || fail "start-limit: 5th restart (upgrade) refused: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V11}" ]] || fail "start-limit: HEAD is not 1.1.0 after the 5th restart"

# The auto-rollback resets first: two starts already spent, the upgrade's
# restart is the 3rd and its rollback's the 4th, which must still start
make_case start-limit-autorollback "${C_V1}"
echo 2 >"${CASE}/starts"
echo 1.1.0 >"${CASE}/unhealthy-tag"
if upgrade joustmania-v1.1.0; then fail "start-limit-autorollback: reported success"; fi
grep -q "failed; rolled back to ${C_V1}" "${CASE}/out.log" || fail "start-limit-autorollback: rollback not healthy: $(tail -1 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "start-limit-autorollback: HEAD is not 1.0.0"
[[ "$(grep -c '^sudo systemctl restart joustmania' "${CASE}/calls.log")" == 2 ]] || fail "start-limit-autorollback: not 2 restarts"
# every restart directly follows a reset-failed
grep -E '^sudo systemctl (reset-failed|restart) ' "${CASE}/calls.log" | awk '
  /restart/ { if (prev !~ /reset-failed/) bad = 1 } { prev = $0 } END { exit bad }' \
  || fail "start-limit-autorollback: a restart without reset-failed before it"

# A start that fails (Result=exit-code, not refused) is not healthy: the
# upgrade rolls back, with no refusal retry
make_case start-fails-exit-code "${C_V1}"
touch "${CASE}/restart-fail-once"
if upgrade joustmania-v1.1.0; then fail "start-fails-exit-code: reported success"; fi
grep -q "failed; rolled back to ${C_V1}" "${CASE}/out.log" || fail "start-fails-exit-code: not rolled back: $(tail -1 "${CASE}/out.log")"
grep -q 'refused by systemd' "${CASE}/out.log" && fail "start-fails-exit-code: a failed start reported as refused"
[[ "$(head_of)" == "${C_V1}" ]] || fail "start-fails-exit-code: HEAD is not back on 1.0.0"
[[ "$(grep -c '^sudo systemctl restart joustmania' "${CASE}/calls.log")" == 2 ]] || fail "start-fails-exit-code: not exactly 2 restarts"

# A start systemd still refuses (reset-failed has no effect): reset, retry once,
# then reported as refused with the recovery command; never judged unhealthy,
# never rolled back
make_case start-limit-refused "${C_V1}"
echo 3 >"${CASE}/starts"
touch "${CASE}/reset-noop"
if upgrade joustmania-v1.1.0; then fail "start-limit-refused: reported success"; fi
grep -q 'refused by systemd (start-limit-hit)' "${CASE}/out.log" || fail "start-limit-refused: not reported as refused: $(tail -2 "${CASE}/out.log")"
grep -qF 'sudo systemctl reset-failed joustmania && sudo systemctl start joustmania' "${CASE}/out.log" \
  || fail "start-limit-refused: no recovery command"
grep -qE 'not healthy|; rolling back|rolled back' "${CASE}/out.log" && fail "start-limit-refused: a refused start was judged or rolled back"
[[ "$(head_of)" == "${C_V11}" ]] || fail "start-limit-refused: HEAD moved off the target (rolled back)"
[[ "$(grep -c '^sudo systemctl restart joustmania' "${CASE}/calls.log")" == 2 ]] || fail "start-limit-refused: not retried exactly once"
grep -q 'image rm' "${CASE}/calls.log" && fail "start-limit-refused: images pruned"
# --status shows start-limit-hit with the recovery command
upgrade --status || fail "start-limit-refused: --status failed"
grep -q 'start-limit-hit' "${CASE}/out.log" || fail "start-limit-refused: --status does not show start-limit-hit"
grep -qF 'sudo systemctl reset-failed joustmania && sudo systemctl start joustmania' "${CASE}/out.log" \
  || fail "start-limit-refused: --status shows no recovery command"
# a refused --rollback is reported as refused too
: >"${CASE}/calls.log"
if upgrade --rollback; then fail "start-limit-refused: --rollback reported success"; fi
grep -q 'refused by systemd (start-limit-hit)' "${CASE}/out.log" || fail "start-limit-refused: --rollback refusal not reported: $(tail -2 "${CASE}/out.log")"
grep -q 'did not come up healthy' "${CASE}/out.log" && fail "start-limit-refused: --rollback refusal reported as unhealthy"
# the refused rollback keeps its target: --status warns, the next --rollback
# finishes on 1.0.0 and never toggles back to 1.1.0
[[ "$(head_of)" == "${C_V1}" && "$(rel rollback_to)" == "${C_V1}" ]] || fail "start-limit-refused: rollback_to not kept after a refused start"
upgrade --status || fail "start-limit-refused: --status failed after the refused --rollback"
grep -q "WARN: a rollback to ${C_V1} was cut off" "${CASE}/out.log" || fail "start-limit-refused: --status does not warn about the unfinished rollback"
rm -f "${CASE}/reset-noop"
upgrade --rollback || fail "start-limit-refused: finishing --rollback failed: $(tail -2 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_V1}" ]] || fail "start-limit-refused: the finishing --rollback toggled back to 1.1.0"
grep -q '^rollback_to=' "${REPO}/.joustmania-release" && fail "start-limit-refused: rollback_to left after the finishing --rollback"
# a healthy unit: no hint
make_case start-limit-status-ok "${C_V1}"
upgrade --status || fail "start-limit-status-ok: --status failed"
grep -q 'start-limit-hit' "${CASE}/out.log" && fail "start-limit-status-ok: hint shown for a healthy unit"

# --- 7b. the migration: a version that ran from a branch is restored on it ---
# The pre-migration Pi runs branch main with .env.local IMAGE_TAG=stale-local
make_case migrate-unhealthy
printf '%s:stale-local\n%s:stale-local\n' "${GC}" "${AG}" >>"${CASE}/images.local"
echo 1.1.0 >"${CASE}/unhealthy-tag"
if upgrade joustmania-v1.1.0; then fail "migrate-unhealthy: reported success"; fi
[[ "$(g -C "${REPO}" symbolic-ref -q --short HEAD)" == main ]] || fail "migrate-unhealthy: not back on branch main"
[[ "$(head_of)" == "${C_V1}" ]] || fail "migrate-unhealthy: not back at the branch commit"
grep 'compose.* ps ' "${CASE}/calls.log" | tail -n 1 | grep -q 'tag=stale-local' \
  || fail "migrate-unhealthy: the rollback did not run the tag it ran with (stale-local)"
[[ "$(rel current_branch)" == main && "$(rel current_tag)" == stale-local ]] || fail "migrate-unhealthy: record lacks branch main / stale-local"

make_case migrate-rollback
printf '%s:stale-local\n%s:stale-local\n' "${GC}" "${AG}" >>"${CASE}/images.local"
upgrade "${C_MAIN}" || fail "migrate-rollback: upgrade failed: $(tail -2 "${CASE}/out.log")"
[[ "$(rel previous_branch)" == main && "$(rel previous_tag)" == stale-local ]] || fail "migrate-rollback: previous branch/tag not recorded"
# .env.local changed since: compose would resolve images that are not on the Pi
cp "${REPO}/.env.local" "${CASE}/env.local.orig"
printf 'IMAGE_TAG=never-pulled\n' >"${REPO}/.env.local"
cp "${REPO}/.joustmania-release" "${CASE}/release.before"
: >"${CASE}/calls.log"
echo 000 >"${CASE}/http_code"
if upgrade --rollback; then fail "migrate-rollback: rolled back to images that are not on the Pi"; fi
grep -q "not on the Pi:.*${GC}:never-pulled" "${CASE}/out.log" || fail "migrate-rollback: resolved images not checked: $(tail -1 "${CASE}/out.log")"
[[ "$(head_of)" == "${C_MAIN}" ]] || fail "migrate-rollback: not switched back to the running version"
g -C "${REPO}" symbolic-ref -q HEAD >/dev/null && fail "migrate-rollback: left on a branch after the refusal"
grep -q restart "${CASE}/calls.log" && fail "migrate-rollback: the unit was restarted"
cmp -s "${CASE}/release.before" "${REPO}/.joustmania-release" || fail "migrate-rollback: record changed"
# switch_back_and_restore returned 2: the command says nothing changed
grep -q "nothing changed: ${C_MAIN} still runs" "${CASE}/out.log" || fail "migrate-rollback: no 'nothing changed': $(tail -1 "${CASE}/out.log")"
grep -q 'state failed' "${CASE}/out.log" && fail "migrate-rollback: says state failed although nothing changed"
cp "${CASE}/env.local.orig" "${REPO}/.env.local"
upgrade --rollback || fail "migrate-rollback: --rollback failed: $(tail -2 "${CASE}/out.log")"
[[ "$(g -C "${REPO}" symbolic-ref -q --short HEAD)" == main && "$(head_of)" == "${C_V1}" ]] \
  || fail "migrate-rollback: not back on branch main at the branch commit"
grep 'compose.* ps ' "${CASE}/calls.log" | tail -n 1 | grep -q 'tag=stale-local' \
  || fail "migrate-rollback: --rollback did not run stale-local"

# --rollback refuses to move a branch that moved on since the migration:
# HEAD, the branch and the record unchanged, no restart
make_case rollback-branch-moved
printf '%s:stale-local\n%s:stale-local\n' "${GC}" "${AG}" >>"${CASE}/images.local"
upgrade "${C_MAIN}" || fail "rollback-branch-moved: upgrade failed: $(tail -2 "${CASE}/out.log")"
g -C "${REPO}" branch -f main "${C_V11}"
cp "${REPO}/.joustmania-release" "${CASE}/release.before"
: >"${CASE}/calls.log"
if upgrade --rollback; then fail "rollback-branch-moved: moved the branch back"; fi
grep -q "branch main is at ${C_V11}" "${CASE}/out.log" || fail "rollback-branch-moved: refusal not reported: $(tail -2 "${CASE}/out.log")"
grep -q "nothing changed: ${C_MAIN} still runs" "${CASE}/out.log" || fail "rollback-branch-moved: no 'nothing changed'"
[[ "$(head_of)" == "${C_MAIN}" ]] || fail "rollback-branch-moved: HEAD moved"
g -C "${REPO}" symbolic-ref -q HEAD >/dev/null && fail "rollback-branch-moved: HEAD left on a branch"
[[ "$(g -C "${REPO}" rev-parse main)" == "${C_V11}" ]] || fail "rollback-branch-moved: branch main moved"
grep -q restart "${CASE}/calls.log" && fail "rollback-branch-moved: the unit was restarted"
cmp -s "${CASE}/release.before" "${REPO}/.joustmania-release" || fail "rollback-branch-moved: record changed"

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
shell_tag="$(IMAGE_TAG=latest pin_tag 2>"${CASE}/pin.err")"
[[ "${shell_tag}" == 1.1.0 ]] || fail "pin: a shell IMAGE_TAG beat the pin: ${shell_tag}"
grep -q 'IMAGE_TAG=latest from the shell is ignored.*1.1.0' "${CASE}/pin.err" || fail "pin: no stderr note for the overridden shell IMAGE_TAG"
IMAGE_TAG=1.1.0 pin_tag >/dev/null 2>"${CASE}/pin.err"
[[ -s "${CASE}/pin.err" ]] && fail "pin: stderr note although the shell IMAGE_TAG equals the pin"
g -C "${REPO}" checkout -q --detach "${C_MAIN}"
[[ "$(pin_tag)" == "${C_MAIN}" ]] || fail "pin: untagged commit does not give the sha: $(pin_tag)"
g -C "${REPO}" checkout -q main
[[ "$(pin_tag)" == stale-local ]] || fail "pin: a branch checkout must not derive a tag: $(pin_tag)"
g -C "${REPO}" checkout -q --detach "${C_V11}"
mkdir -p "${CASE}/asound"
printf '#!/usr/bin/env bash\nexit 0\n' >"${CASE}/bin/amixer"
chmod +x "${CASE}/bin/amixer"
echo "${GC}:1.1.0" >>"${CASE}/images.local"
env PATH="${CASE}/bin:${PATH}" JOUSTMANIA_DOCKER_WAIT_S=1 JOUSTMANIA_ASOUND_DIR="${CASE}/asound" \
  JOUSTMANIA_STATUS_DIR="${CASE}/status" JOUSTMANIA_UNIT_REV=2 JOUSTMANIA_GAME_READY_S=1 \
  bash "${REPO}/scripts/setup/joustmania-start.sh" >"${CASE}/start.log" 2>&1 || fail "pin: start script failed"
grep -q 'IMAGE_TAG 1.1.0' "${CASE}/start.log" || fail "pin: start script does not run the derived tag"
grep -q 'WARN: .env.local sets IMAGE_TAG' "${CASE}/start.log" || fail "pin: start script does not WARN about IMAGE_TAG in .env.local"

if ((fails > 0)); then
  echo "test-joustmania-upgrade: ${fails} failure(s)"
  exit 1
fi
echo "test-joustmania-upgrade: OK"
