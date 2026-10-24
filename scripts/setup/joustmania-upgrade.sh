#!/bin/bash
# Move the Pi to another pinned version, with rollback (UC-060 A2, A3, BR-003;
# FR-182, C-040; #2027).
#
#   ./scripts/setup/joustmania-upgrade.sh [--no-rollback] <joustmania-vX.Y.Z | X.Y.Z | 40-hex sha>
#   ./scripts/setup/joustmania-upgrade.sh --rollback
#   ./scripts/setup/joustmania-upgrade.sh --status
#
# Upgrade:
#   1. Refuse `latest`, branch names, short shas and anything below the
#      MIN_PINNABLE floor; nothing changes.
#   2. Preconditions, nothing changes yet: run as the checkout owner (not root),
#      clean tracked tree, `git worktree prune`, the registry answers /v2/ with
#      HTTP 200 or 401, `git fetch --tags` and the target resolves.
#   3. Prepare: check the target out in a temporary worktree (removed by a trap),
#      pull its images with its own compose file and the active COMPOSE_PROFILES
#      (shell, .env.local, or the unit's Environment=), and abort listing every
#      image of `config --images` that is still missing. The running version is
#      untouched.
#   4. Switch: `git checkout --detach <target>`, then record current and
#      previous (ref, branch, image tag, images) in .joustmania-release. The
#      checkout is the single switch: the image tag derives from HEAD
#      (lib-joustmania.sh), no env file is written.
#   5. Apply: `sudo systemctl reset-failed joustmania` (operator restarts do not
#      spend the unit's start limit, BR-006), `sudo systemctl restart
#      joustmania`, then wait until every game service is running (and healthy
#      where it has a healthcheck). A start systemd refuses (start-limit-hit)
#      is reset and retried once, then reported as refused: never rolled back.
#   6. Not healthy within the timeout: roll back to the previous version
#      (A3) unless --no-rollback. Healthy: remove superseded images of the
#      joustmania repositories the two recorded versions use (nothing else).
# --rollback: switch to the recorded version that is not HEAD, with the images
# on the Pi (no registry, no fetch), so it works offline. A version that ran
# from a branch (the one-time migration) gets its branch back, so compose
# resolves the tag it ran with; the images compose resolves after the switch
# must all be on the Pi, else the checkout is switched back and nothing runs.
# The record names the target (rollback_to=) from before the checkout until the
# restart is judged, so a rollback cut off in between is finished by the next
# --rollback, never toggled back. A refused or failed switch says "nothing
# changed"; --status WARNs when HEAD is neither recorded version, and re-running
# the same upgrade repairs a record an upgrade cut off after its checkout left.
set -euo pipefail

# The floor (UC-060 BR-003). MIN_PINNABLE is the first pinnable release,
# recorded at the release cut (maintainer step); empty until then. The commit
# floor is #2040 (the agent image is tagged on release): a target must contain
# it, and it must contain this command, or a rollback from it is impossible.
MIN_PINNABLE=""
MIN_PINNABLE_COMMIT="02e7752df741774aa0d0cc3b24bd69acdd036de8"

UNIT="joustmania"
RELEASE_FILE=".joustmania-release"
REGISTRY_URL="${JOUSTMANIA_REGISTRY_URL:-https://ghcr.io/v2/}"
REGISTRY_TIMEOUT="${JOUSTMANIA_REGISTRY_TIMEOUT:-10}"
FETCH_TIMEOUT="${JOUSTMANIA_FETCH_TIMEOUT:-60}"
PULL_TIMEOUT="${JOUSTMANIA_PULL_TIMEOUT:-900}"
HEALTH_TIMEOUT="${JOUSTMANIA_HEALTH_TIMEOUT:-180}"
HEALTH_POLL="${JOUSTMANIA_HEALTH_POLL:-5}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOUSTMANIA_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
# shellcheck source=lib-joustmania.sh
source "$SCRIPT_DIR/lib-joustmania.sh"

PREP_WT=""

die() {
    echo "joustmania-upgrade: $*" >&2
    exit 1
}
say() { echo "joustmania-upgrade: $*"; }

usage() {
    sed -n '5,7p' "${BASH_SOURCE[0]}" | sed 's/^# *//'
    exit 2
}

cleanup() {
    if [[ -n "$PREP_WT" ]]; then
        git -C "$JOUSTMANIA_DIR" worktree remove --force "$PREP_WT" >/dev/null 2>&1 || rm -rf "$PREP_WT"
        git -C "$JOUSTMANIA_DIR" worktree prune >/dev/null 2>&1 || true
    fi
}

# compose_in <dir> <args...>: the Pi compose wrapper of the checkout at <dir>
compose_in() {
    local dir="$1"
    shift
    "$dir/scripts/setup/joustmania-compose.sh" "$@"
}

# Images of a checkout as compose resolves them (profiles, pin, env files),
# one per line, digests stripped.
images_of() {
    compose_in "$1" config --images | sed 's/@.*$//' | sort -u
}

# release_get <key>: a value of .joustmania-release (empty when absent)
release_get() {
    sed -n "s/^$1=//p" "$JOUSTMANIA_DIR/$RELEASE_FILE" 2>/dev/null | head -n 1
}

# write_release <cur_ref> <cur_branch> <cur_tag> <cur_images> <prev_ref> <prev_branch> <prev_tag> <prev_images> [<rollback_to>]
# Images are space-separated; a branch is empty for a detached (pinned)
# checkout. <rollback_to> marks a rollback in progress (see roll_back_to_previous).
# Written to a temp file in the same directory, then renamed, so a crash never
# leaves half a record.
write_release() {
    local tmp="$JOUSTMANIA_DIR/$RELEASE_FILE.tmp"
    printf 'current_ref=%s\ncurrent_branch=%s\ncurrent_tag=%s\ncurrent_images=%s\nprevious_ref=%s\nprevious_branch=%s\nprevious_tag=%s\nprevious_images=%s\n' \
        "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" >"$tmp" || return 1
    [[ -z "${9:-}" ]] || printf 'rollback_to=%s\n' "$9" >>"$tmp" || return 1
    mv -f "$tmp" "$JOUSTMANIA_DIR/$RELEASE_FILE"
}

# write_release_raw <content>: put a saved record back as it was
write_release_raw() {
    local tmp="$JOUSTMANIA_DIR/$RELEASE_FILE.tmp"
    printf '%s\n' "$1" >"$tmp" || return 1
    mv -f "$tmp" "$JOUSTMANIA_DIR/$RELEASE_FILE"
}

# COMPOSE_PROFILES as the unit runs it: the shell wins, .env.local is read by
# compose itself, else the unit's Environment= (a `systemctl edit` drop-in).
load_unit_profiles() {
    [[ -n "${COMPOSE_PROFILES:-}" ]] && return 0
    joustmania_env_local_sets "$JOUSTMANIA_DIR/.env.local" COMPOSE_PROFILES && return 0
    local env word
    env="$(systemctl show -p Environment --value "$UNIT" 2>/dev/null || true)"
    for word in $env; do
        if [[ "$word" == COMPOSE_PROFILES=* ]]; then
            export COMPOSE_PROFILES="${word#COMPOSE_PROFILES=}"
            say "using COMPOSE_PROFILES=$COMPOSE_PROFILES from the $UNIT unit"
        fi
    done
}

warn_overrides() {
    if joustmania_env_local_sets "$JOUSTMANIA_DIR/.env.local" IMAGE_TAG; then
        say "WARN: .env.local sets IMAGE_TAG; it is ignored (the pinned checkout decides, UC-060 BR-003). Remove it."
    fi
    if [[ -n "${OTEL_COLLECTOR_TAG:-}" ]] || joustmania_env_local_sets "$JOUSTMANIA_DIR/.env.local" OTEL_COLLECTOR_TAG; then
        say "WARN: OTEL_COLLECTOR_TAG is set; the collector image does not follow the pin."
    fi
}

registry_reachable() {
    local code
    code="$(curl -s -o /dev/null -m "$REGISTRY_TIMEOUT" -w '%{http_code}' "$REGISTRY_URL" 2>/dev/null || true)"
    [[ "$code" == 200 || "$code" == 401 ]]
}

# resolve_target <arg>: sets TARGET_REF (the git ref to check) or dies.
resolve_target_syntax() {
    local arg="$1"
    case "$arg" in
        joustmania-v[0-9]*) TARGET_REF="$arg" ;;
        v[0-9]*) TARGET_REF="joustmania-$arg" ;;
        [0-9]*.[0-9]*.[0-9]*) TARGET_REF="joustmania-v$arg" ;;
        *)
            if [[ "$arg" =~ ^[0-9a-f]{40}$ ]]; then
                TARGET_REF="$arg"
                return 0
            fi
            die "refusing '$arg': pin a release tag (joustmania-vX.Y.Z) or an exact 40-character commit sha; never latest, main or a branch (UC-060 BR-003). Nothing changed."
            ;;
    esac
    [[ "$TARGET_REF" =~ ^joustmania-v[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
        die "refusing '$arg': not a release tag joustmania-vX.Y.Z (UC-060 BR-003). Nothing changed."
}

# check_floor <commit> [<X.Y.Z>]: dies when the target is below MIN_PINNABLE.
check_floor() {
    local commit="$1" version="${2:-}" lowest
    if [[ -n "$MIN_PINNABLE" && -n "$version" ]]; then
        lowest="$(printf '%s\n%s\n' "$MIN_PINNABLE" "$version" | sort -V | head -n 1)"
        [[ "$lowest" == "$MIN_PINNABLE" ]] ||
            die "refusing $version: older than MIN_PINNABLE $MIN_PINNABLE (UC-060 BR-003). Nothing changed."
    fi
    if [[ -n "$MIN_PINNABLE" ]] && git -C "$JOUSTMANIA_DIR" rev-parse -q --verify "refs/tags/joustmania-v$MIN_PINNABLE" >/dev/null; then
        git -C "$JOUSTMANIA_DIR" merge-base --is-ancestor "joustmania-v$MIN_PINNABLE" "$commit" ||
            die "refusing $commit: it does not contain MIN_PINNABLE joustmania-v$MIN_PINNABLE (UC-060 BR-003). Nothing changed."
    fi
    git -C "$JOUSTMANIA_DIR" merge-base --is-ancestor "$MIN_PINNABLE_COMMIT" "$commit" 2>/dev/null ||
        die "refusing $commit: older than the pinnable floor (it lacks #2040, so its release has no agent image; UC-060 BR-003). Nothing changed."
    git -C "$JOUSTMANIA_DIR" cat-file -e "$commit:scripts/setup/joustmania-upgrade.sh" 2>/dev/null ||
        die "refusing $commit: it has no scripts/setup/joustmania-upgrade.sh, so it could not be upgraded or rolled back from (UC-060 BR-003). Nothing changed."
}

common_preconditions() {
    [[ "$(id -u)" -ne 0 ]] || die "run as the checkout owner, not root (sudo is used only for the unit restart)"
    cd "$JOUSTMANIA_DIR"
    [[ -z "$(git status --porcelain --untracked-files=no)" ]] ||
        die "the checkout has local changes to tracked files; commit or revert them first. Nothing changed."
    git worktree prune
}

# wait_healthy: every active game service has a running container that is
# healthy when it has a healthcheck, within HEALTH_TIMEOUT.
wait_healthy() {
    local deadline=$((SECONDS + HEALTH_TIMEOUT)) services ps svc line state health bad
    # Fail closed: no service list, or one without a game service, is not healthy
    if ! services="$(compose_in "$JOUSTMANIA_DIR" config --services 2>/dev/null)"; then
        say "compose config --services failed; not healthy"
        return 1
    fi
    for svc in "${JOUSTMANIA_GAME_SERVICES[@]}"; do
        grep -qxF -- "$svc" <<<"$services" && break
        svc=""
    done
    if [[ -z "$svc" ]]; then
        say "compose lists no game service; not healthy"
        return 1
    fi
    while :; do
        bad=""
        ps="$(compose_in "$JOUSTMANIA_DIR" ps -a --format '{{.Service}}|{{.State}}|{{.Health}}' 2>/dev/null || true)"
        for svc in "${JOUSTMANIA_GAME_SERVICES[@]}"; do
            grep -qxF -- "$svc" <<<"$services" || continue
            line="$(grep -m 1 "^$svc|" <<<"$ps" || true)"
            IFS='|' read -r _ state health <<<"$line"
            if [[ "$state" != running || (-n "$health" && "$health" != healthy) ]]; then
                bad+=" $svc(${state:-missing}${health:+/$health})"
            fi
        done
        if [[ -z "$bad" ]]; then
            return 0
        fi
        if ((SECONDS >= deadline)); then
            say "game services not healthy:$bad"
            return 1
        fi
        sleep "$HEALTH_POLL"
    done
}

RECOVER_CMD="sudo systemctl reset-failed $UNIT && sudo systemctl start $UNIT"

# start_refused: systemd refused the last start of the unit (start limit)
start_refused() {
    [[ "$(systemctl show -p Result --value "$UNIT" 2>/dev/null || true)" == start-limit-hit ]]
}

# restart_unit: an operator restart (UC-060 BR-006). The unit's start limit
# (3 in 900 s) is for crash loops of automatic retries, not for deliberate
# restarts, so reset it first; it is never weakened in the unit itself. A start
# systemd still refuses is reset and retried once.
# Returns 0 started, 1 failed to start, 3 refused by systemd (start-limit-hit).
restart_unit() {
    sudo systemctl reset-failed "$UNIT" || say "WARN: systemctl reset-failed $UNIT failed"
    sudo systemctl restart "$UNIT" && return 0
    start_refused || return 1
    say "systemd refused the start (start-limit-hit); resetting the start limit and retrying once"
    sudo systemctl reset-failed "$UNIT" || say "WARN: systemctl reset-failed $UNIT failed"
    sudo systemctl restart "$UNIT" && return 0
    start_refused && return 3
    return 1
}

# apply: restart the unit and judge the game services. Returns 0 healthy,
# 1 failed to start or not healthy, 3 the start was refused by systemd: the
# version was never judged, so callers must not roll back because of it.
apply() {
    say "restarting the $UNIT unit"
    local rc=0
    restart_unit || rc=$?
    case "$rc" in
        0) wait_healthy ;;
        3)
            say "the $UNIT unit start was refused by systemd (start-limit-hit), not judged; recover with: $RECOVER_CMD"
            return 3
            ;;
        *)
            say "the $UNIT unit failed to start"
            return 1
            ;;
    esac
}

# switch_to <commit>: the single switch (UC-060 A2 step 2)
switch_to() {
    git -C "$JOUSTMANIA_DIR" checkout -q --detach "$1"
}

# restore_checkout <commit> <branch>: back to a recorded version as it ran:
# detached when <branch> is empty, else on <branch> at <commit> (a version that
# ran from a branch derives no tag; compose takes it from the env files).
# Refuses when the branch has moved on since.
restore_checkout() {
    local ref="$1" branch="$2" tip
    if [[ -z "$branch" ]]; then
        switch_to "$ref"
        return
    fi
    git -C "$JOUSTMANIA_DIR" check-ref-format --branch "$branch" >/dev/null 2>&1 || {
        say "recorded branch '$branch' is not a valid branch name"
        return 1
    }
    tip="$(git -C "$JOUSTMANIA_DIR" rev-parse -q --verify "refs/heads/$branch" 2>/dev/null || true)"
    if [[ -n "$tip" && "$tip" != "$ref" ]]; then
        say "branch $branch is at $tip, not at the recorded $ref; not moving it"
        return 1
    fi
    git -C "$JOUSTMANIA_DIR" checkout -q -B "$branch" "$ref"
}

# missing_images <images...>: prints the ones not on the Pi (space-led)
missing_images() {
    local img missing=""
    for img in "$@"; do
        docker image inspect "$img" >/dev/null 2>&1 || missing+=" $img"
    done
    printf '%s' "$missing"
}

# prune_images: remove local images of the repositories the recorded versions
# use whose tag neither of them uses. Fails closed: no record, or a record
# without both image lists, removes nothing. Images of other repositories are
# never touched (UC-060 A2 step 4).
prune_images() {
    local current previous keep repos img repo
    current="$(release_get current_images)"
    previous="$(release_get previous_images)"
    if [[ -z "$current" || -z "$previous" ]]; then
        say "no complete $RELEASE_FILE record; removing no images"
        return 0
    fi
    keep="$(tr ' ' '\n' <<<"$current $previous" | sed '/^$/d' | sort -u)"
    # Only joustmania repositories (UC-060 A2 step 4): grafana, flagd, jaeger and
    # the other third-party images are never removed
    repos="$(sed 's/:[^:/]*$//' <<<"$keep" | grep '/joustmania/' | sort -u || true)"
    [[ -n "$repos" ]] || return 0
    while IFS= read -r img; do
        [[ -n "$img" && "$img" != *"<none>"* ]] || continue
        repo="${img%:*}"
        grep -qxF -- "$repo" <<<"$repos" || continue
        grep -qxF -- "$img" <<<"$keep" && continue
        say "removing superseded image $img"
        docker image rm "$img" >/dev/null 2>&1 || say "could not remove $img (in use?); kept"
    done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || true)
}

current_tag_of() { # effective image tag of the running checkout (game-coordinator's)
    local gc
    gc="$(grep -m 1 '/game-coordinator-service:' <<<"$1" || true)"
    [[ -n "$gc" ]] && echo "${gc##*:}"
}

do_status() {
    cd "$JOUSTMANIA_DIR"
    if [[ -f "$RELEASE_FILE" ]]; then
        say "current:  $(release_get current_ref) (image tag $(release_get current_tag))"
        say "previous: $(release_get previous_ref) (image tag $(release_get previous_tag))"
        local head pending
        head="$(git rev-parse -q --verify HEAD 2>/dev/null || true)"
        pending="$(release_get rollback_to)"
        if [[ -n "$pending" ]]; then
            say "WARN: a rollback to $pending was cut off or its start refused; run --rollback to finish it (it stays on $pending)"
        fi
        if [[ "$head" != "$(release_get current_ref)" && "$head" != "$(release_get previous_ref)" ]]; then
            say "WARN: HEAD $head is neither recorded version, so --rollback is unavailable (an upgrade cut off after its checkout?); see the manual recovery in docs/DEVELOPMENT.md (UC-060 A3)"
        fi
    else
        say "no $RELEASE_FILE yet (no upgrade has run)"
    fi
    local tag
    if tag="$(joustmania_pin_image_tag "$JOUSTMANIA_DIR")"; then
        say "checkout pinned: IMAGE_TAG $tag"
    else
        say "WARN: checkout not pinned (HEAD on a branch, UC-060 A4)"
    fi
    warn_overrides
    if start_refused; then
        say "WARN: the $UNIT unit is start-limit-hit (systemd refuses to start it); recover with: $RECOVER_CMD"
    fi
}

# repair_stale_record <commit>: an upgrade cut off between its checkout and its
# record write leaves HEAD at <commit> but the record naming the version
# before. Record <commit> as current and the recorded current as previous, so
# --rollback works again. Without a record (the migration) the previous branch
# is unknown: WARN and point at the manual recovery.
repair_stale_record() {
    local commit="$1" cur_ref imgs tag
    if [[ ! -f "$JOUSTMANIA_DIR/$RELEASE_FILE" ]]; then
        say "WARN: no $RELEASE_FILE, so --rollback is unavailable; see the manual recovery in docs/DEVELOPMENT.md (UC-060 A3)"
        return 0
    fi
    cur_ref="$(release_get current_ref)"
    [[ "$cur_ref" != "$commit" && -z "$(release_get rollback_to)" ]] || return 0
    load_unit_profiles
    imgs="$(images_of "$JOUSTMANIA_DIR")" || die "compose config --images failed; the stale $RELEASE_FILE was kept. Nothing changed."
    tag="$(joustmania_pin_image_tag "$JOUSTMANIA_DIR")"
    write_release "$commit" "" "$tag" "$(tr '\n' ' ' <<<"$imgs" | sed 's/ $//')" \
        "$cur_ref" "$(release_get current_branch)" "$(release_get current_tag)" "$(release_get current_images)" ||
        die "could not write $RELEASE_FILE"
    say "repaired the stale $RELEASE_FILE: current $commit, previous $cur_ref (an earlier upgrade was cut off after its checkout; the unit was not restarted by this run)"
}

do_upgrade() {
    local arg="$1" no_rollback="$2" commit version="" tag prev_ref prev_branch prev_imgs prev_tag new_imgs missing img
    resolve_target_syntax "$arg"
    common_preconditions
    registry_reachable || die "registry $REGISTRY_URL unreachable (needs HTTP 200 or 401); nothing changed"
    timeout "$FETCH_TIMEOUT" git fetch -q --tags origin || die "git fetch --tags failed; nothing changed"
    commit="$(git rev-parse -q --verify "$TARGET_REF^{commit}" 2>/dev/null)" ||
        die "unknown target $TARGET_REF (no such tag or commit after fetch); nothing changed"
    [[ "$TARGET_REF" == joustmania-v* ]] && version="${TARGET_REF#joustmania-v}"
    check_floor "$commit" "$version"

    prev_ref="$(git rev-parse HEAD)"
    prev_branch="$(git symbolic-ref -q --short HEAD || true)"
    if [[ "$commit" == "$prev_ref" ]] && joustmania_pin_image_tag "$JOUSTMANIA_DIR" >/dev/null; then
        repair_stale_record "$commit"
        say "already at $TARGET_REF ($commit); nothing to do"
        return 0
    fi
    load_unit_profiles
    warn_overrides
    prev_imgs="$(images_of "$JOUSTMANIA_DIR")" || die "compose config --images failed for the current checkout; nothing changed"
    prev_tag="$(current_tag_of "$prev_imgs")"

    # Prepare in a temporary worktree: the running version is untouched
    PREP_WT="$(mktemp -d "${TMPDIR:-/tmp}/joustmania-upgrade.XXXXXX")"
    rmdir "$PREP_WT"
    trap cleanup EXIT
    git worktree add -q --detach "$PREP_WT" "$commit" || die "could not check the target out in a temporary worktree; nothing changed"
    [[ -f .env.local ]] && ln -s "$JOUSTMANIA_DIR/.env.local" "$PREP_WT/.env.local"
    tag="$(joustmania_pin_image_tag "$PREP_WT")" || die "could not derive the image tag of $commit; nothing changed"
    say "pulling the images of $TARGET_REF (image tag $tag) while the current version keeps running"
    timeout "$PULL_TIMEOUT" "$PREP_WT/scripts/setup/joustmania-compose.sh" pull ||
        say "pull reported errors; checking which images are present"
    new_imgs="$(images_of "$PREP_WT")" || die "compose config --images failed for $TARGET_REF; nothing changed"
    missing=""
    while IFS= read -r img; do
        [[ -n "$img" ]] || continue
        docker image inspect "$img" >/dev/null 2>&1 || missing+=" $img"
    done <<<"$new_imgs"
    [[ -z "$missing" ]] || die "images missing after the pull:$missing; nothing changed"
    cleanup
    PREP_WT=""

    switch_to "$commit" || die "git checkout of $commit failed; nothing changed: $prev_ref still runs and the record is unchanged. Check the tree before retrying."
    write_release "$commit" "" "$tag" "$(tr '\n' ' ' <<<"$new_imgs" | sed 's/ $//')" \
        "$prev_ref" "$prev_branch" "$prev_tag" "$(tr '\n' ' ' <<<"$prev_imgs" | sed 's/ $//')"
    if ! git diff --quiet "$prev_ref" "$commit" -- scripts/setup/joustmania.service; then
        say "unit file changed: run sudo $JOUSTMANIA_DIR/scripts/setup/install_autostart.sh"
    fi
    local apply_rc=0
    apply || apply_rc=$?
    if ((apply_rc == 0)); then
        say "now at $TARGET_REF (image tag $tag); previous $prev_ref kept for --rollback"
        prune_images
        return 0
    fi
    if ((apply_rc == 3)); then
        # Never roll back because of a refused start (UC-060 BR-006)
        die "the start of $TARGET_REF was refused by systemd (start-limit-hit); not rolling back. $TARGET_REF is checked out (previous $prev_ref kept for --rollback); recover with: $RECOVER_CMD"
    fi
    if [[ "$no_rollback" == 1 ]]; then
        die "$TARGET_REF is not healthy; left in place for inspection (--no-rollback). Roll back with --rollback."
    fi
    say "$TARGET_REF is not healthy; rolling back to $prev_ref"
    local rc=0
    roll_back_to_previous || rc=$?
    case "$rc" in
        0) die "upgrade to $TARGET_REF failed; rolled back to $prev_ref" ;;
        2) die "upgrade to $TARGET_REF failed and the rollback to $prev_ref was not applied (nothing switched back); the unhealthy $TARGET_REF stays checked out (state failed)" ;;
        3) die "upgrade to $TARGET_REF failed; $prev_ref is checked out but its start was refused by systemd (start-limit-hit); recover with: $RECOVER_CMD (or re-run --rollback, which finishes on $prev_ref)" ;;
        *) die "rollback to $prev_ref did not come up healthy either (state failed)" ;;
    esac
}

# rollback_plan: from .joustmania-release and HEAD, sets T_* (the target) and
# H_* (the other recorded version). The record names two versions; HEAD must be
# one of them. A rollback in progress (rollback_to=<ref>, written before the
# checkout and cleared after the restart) keeps its target, so a rollback cut
# off after its checkout finishes on the version it was moving to and never
# toggles back. Otherwise the target is the recorded version that is not HEAD.
rollback_plan() {
    local head cur_ref prev_ref pending
    head="$(git -C "$JOUSTMANIA_DIR" rev-parse -q --verify HEAD 2>/dev/null || true)"
    cur_ref="$(release_get current_ref)"
    prev_ref="$(release_get previous_ref)"
    pending="$(release_get rollback_to)"
    local t h
    [[ -n "$head" && ("$head" == "$cur_ref" || "$head" == "$prev_ref") ]] || return 1
    if [[ -n "$pending" ]]; then
        if [[ "$pending" == "$cur_ref" ]]; then
            t=current h=previous
        elif [[ "$pending" == "$prev_ref" ]]; then
            t=previous h=current
        else
            return 1
        fi
    elif [[ "$head" == "$cur_ref" ]]; then
        t=previous h=current
    else
        t=current h=previous
    fi
    T_REF="$(release_get "${t}_ref")"
    T_BRANCH="$(release_get "${t}_branch")"
    T_TAG="$(release_get "${t}_tag")"
    T_IMGS="$(release_get "${t}_images")"
    H_REF="$(release_get "${h}_ref")"
    H_BRANCH="$(release_get "${h}_branch")"
    H_TAG="$(release_get "${h}_tag")"
    H_IMGS="$(release_get "${h}_images")"
}

# roll_back_to_previous: switch to the target of rollback_plan, check that every
# image compose resolves there is on the Pi (else switch back and fail), record
# the swap, restart. Runs under `||` (set -e is off), so every step checks.
# The record carries rollback_to=<target> from before the checkout until the
# restart has been judged, so a run cut off anywhere in between is finished by
# the next --rollback instead of toggling back (rollback_plan).
# Returns 0 healthy, 1 restarted but not healthy (or stuck between versions),
# 2 not applied: nothing switched or restarted, the record as it was,
# 3 switched and recorded, but systemd refused the start (BR-006); rollback_to
# stays, so the next --rollback finishes on the same target.
roll_back_to_previous() {
    local imgs missing saved
    rollback_plan || {
        say "HEAD is neither recorded version in $RELEASE_FILE"
        return 2
    }
    saved="$(cat "$JOUSTMANIA_DIR/$RELEASE_FILE")"
    write_release "$H_REF" "$H_BRANCH" "$H_TAG" "$H_IMGS" "$T_REF" "$T_BRANCH" "$T_TAG" "$T_IMGS" "$T_REF" || {
        say "could not write $RELEASE_FILE"
        return 2
    }
    if ! restore_checkout "$T_REF" "$T_BRANCH"; then
        say "git checkout of $T_REF failed"
        write_release_raw "$saved" || say "could not restore $RELEASE_FILE"
        return 2
    fi
    if ! imgs="$(images_of "$JOUSTMANIA_DIR")" || [[ -z "$imgs" ]]; then
        say "compose config --images failed at $T_REF; switching back to $H_REF"
        switch_back_and_restore "$saved"
        return
    fi
    # shellcheck disable=SC2086 # one image per word
    missing="$(missing_images $imgs)"
    if [[ -n "$missing" ]]; then
        say "images compose resolves at $T_REF are not on the Pi:$missing; switching back to $H_REF"
        switch_back_and_restore "$saved"
        return
    fi
    write_release "$T_REF" "$T_BRANCH" "$(current_tag_of "$imgs")" "$(tr '\n' ' ' <<<"$imgs" | sed 's/ $//')" \
        "$H_REF" "$H_BRANCH" "$H_TAG" "$H_IMGS" "$T_REF" || {
        say "could not write $RELEASE_FILE"
        return 1
    }
    local rc=0
    apply || rc=$?
    # A start systemd refused was never judged: keep rollback_to, so the next
    # --rollback finishes on this target instead of toggling back (BR-006)
    ((rc == 3)) && return 3
    # The restart was judged: the rollback is over, healthy or not
    write_release_raw "$(grep -v '^rollback_to=' "$JOUSTMANIA_DIR/$RELEASE_FILE")" ||
        say "could not clear rollback_to in $RELEASE_FILE"
    return "$rc"
}

# switch_back_and_restore <saved record>: undo a rollback checkout before any
# restart. Returns 2 (nothing changed) when HEAD is back, else 1.
switch_back_and_restore() {
    if restore_checkout "$H_REF" "$H_BRANCH"; then
        write_release_raw "$1" || say "could not restore $RELEASE_FILE"
        return 2
    fi
    say "could not switch back to $H_REF; HEAD is at $T_REF, the unit was not restarted (state failed)"
    return 1
}

do_rollback() {
    local missing
    common_preconditions
    rollback_plan || die "no rollback target: $RELEASE_FILE is missing, or HEAD is neither recorded version; nothing changed"
    [[ "$T_REF" =~ ^[0-9a-f]{40}$ ]] || die "no previous version recorded in $RELEASE_FILE; nothing changed"
    git cat-file -e "$T_REF^{commit}" 2>/dev/null || die "previous commit $T_REF is not in the local repository; nothing changed"
    # shellcheck disable=SC2086 # one image per word
    missing="$(missing_images $T_IMGS)"
    [[ -z "$missing" ]] || die "previous images not on the Pi:$missing; nothing changed"
    say "rolling back to $T_REF${T_BRANCH:+ (branch $T_BRANCH)}"
    local rc=0 running
    running="$(git rev-parse HEAD)"
    roll_back_to_previous || rc=$?
    case "$rc" in
        0) say "rolled back to $T_REF" ;;
        2) die "rollback to $T_REF not applied; nothing changed: $running still runs" ;;
        3) die "rollback to $T_REF is checked out, but its start was refused by systemd (start-limit-hit); recover with: $RECOVER_CMD (or re-run --rollback, which finishes on $T_REF)" ;;
        *) die "rollback to $T_REF failed or did not come up healthy (state failed)" ;;
    esac
}

main() {
    local no_rollback=0 mode="" target=""
    while (($#)); do
        case "$1" in
            --no-rollback) no_rollback=1 ;;
            --rollback) mode=rollback ;;
            --status) mode=status ;;
            -h | --help) usage ;;
            -*) die "unknown option $1" ;;
            *)
                [[ -z "$target" ]] || usage
                target="$1"
                ;;
        esac
        shift
    done
    case "$mode" in
        status) do_status ;;
        rollback)
            [[ -z "$target" ]] || usage
            do_rollback
            ;;
        *)
            [[ -n "$target" ]] || usage
            do_upgrade "$target" "$no_rollback"
            ;;
    esac
}

# Everything runs from main(): bash has parsed it before the checkout replaces
# this file. Sourcing (tests) defines the functions only.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
    exit $?
fi
