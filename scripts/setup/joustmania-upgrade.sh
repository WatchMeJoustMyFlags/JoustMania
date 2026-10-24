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
#   5. Apply: `sudo systemctl restart joustmania`, then wait until every game
#      service is running (and healthy where it has a healthcheck).
#   6. Not healthy within the timeout: roll back to the previous version
#      (A3) unless --no-rollback. Healthy: remove superseded images of the
#      joustmania repositories the two recorded versions use (nothing else).
# --rollback: switch to the recorded version that is not HEAD, with the images
# on the Pi (no registry, no fetch), so it works offline. A version that ran
# from a branch (the one-time migration) gets its branch back, so compose
# resolves the tag it ran with; the images compose resolves after the switch
# must all be on the Pi, else the checkout is switched back and nothing runs.
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

# write_release <cur_ref> <cur_branch> <cur_tag> <cur_images> <prev_ref> <prev_branch> <prev_tag> <prev_images>
# Images are space-separated; a branch is empty for a detached (pinned)
# checkout. Written after the checkout succeeded, to a temp file in the same
# directory, then renamed, so a crash never leaves half a record.
write_release() {
    local tmp="$JOUSTMANIA_DIR/$RELEASE_FILE.tmp"
    printf 'current_ref=%s\ncurrent_branch=%s\ncurrent_tag=%s\ncurrent_images=%s\nprevious_ref=%s\nprevious_branch=%s\nprevious_tag=%s\nprevious_images=%s\n' \
        "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8" >"$tmp"
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

# apply: restart the unit and judge the game services
apply() {
    say "restarting the $UNIT unit"
    if ! sudo systemctl restart "$UNIT"; then
        say "the $UNIT unit failed to start"
        return 1
    fi
    wait_healthy
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

    switch_to "$commit" || die "git checkout of $commit failed; the record is unchanged. Check the tree before retrying."
    write_release "$commit" "" "$tag" "$(tr '\n' ' ' <<<"$new_imgs" | sed 's/ $//')" \
        "$prev_ref" "$prev_branch" "$prev_tag" "$(tr '\n' ' ' <<<"$prev_imgs" | sed 's/ $//')"
    if ! git diff --quiet "$prev_ref" "$commit" -- scripts/setup/joustmania.service; then
        say "unit file changed: run sudo $JOUSTMANIA_DIR/scripts/setup/install_autostart.sh"
    fi
    if apply; then
        say "now at $TARGET_REF (image tag $tag); previous $prev_ref kept for --rollback"
        prune_images
        return 0
    fi
    if [[ "$no_rollback" == 1 ]]; then
        die "$TARGET_REF is not healthy; left in place for inspection (--no-rollback). Roll back with --rollback."
    fi
    say "$TARGET_REF is not healthy; rolling back to $prev_ref"
    roll_back_to_previous || die "rollback to $prev_ref did not come up healthy either (state failed)"
    die "upgrade to $TARGET_REF failed; rolled back to $prev_ref"
}

# rollback_plan: from .joustmania-release and HEAD, sets T_* (the target: the
# recorded version that is not HEAD) and H_* (the recorded version at HEAD).
# The record names two versions; HEAD must be one of them. That makes --rollback
# HEAD-relative: an interrupted rollback (checkout failed or never ran) still
# targets the other version, never the one that is running.
rollback_plan() {
    local head cur_ref prev_ref
    head="$(git -C "$JOUSTMANIA_DIR" rev-parse -q --verify HEAD 2>/dev/null || true)"
    cur_ref="$(release_get current_ref)"
    prev_ref="$(release_get previous_ref)"
    local t h
    if [[ -n "$head" && "$head" == "$cur_ref" ]]; then
        t=previous h=current
    elif [[ -n "$head" && "$head" == "$prev_ref" ]]; then
        t=current h=previous
    else
        return 1
    fi
    T_REF="$(release_get "${t}_ref")"
    T_BRANCH="$(release_get "${t}_branch")"
    T_IMGS="$(release_get "${t}_images")"
    H_REF="$(release_get "${h}_ref")"
    H_BRANCH="$(release_get "${h}_branch")"
    H_TAG="$(release_get "${h}_tag")"
    H_IMGS="$(release_get "${h}_images")"
}

# roll_back_to_previous: switch to the target of rollback_plan, check that every
# image compose resolves there is on the Pi (else switch back and fail), record
# the swap, restart. Runs under `||` (set -e is off), so every step checks.
roll_back_to_previous() {
    local imgs missing
    rollback_plan || {
        say "HEAD is neither recorded version in $RELEASE_FILE"
        return 1
    }
    restore_checkout "$T_REF" "$T_BRANCH" || {
        say "git checkout of $T_REF failed"
        return 1
    }
    if ! imgs="$(images_of "$JOUSTMANIA_DIR")" || [[ -z "$imgs" ]]; then
        say "compose config --images failed at $T_REF; switching back to $H_REF"
        restore_checkout "$H_REF" "$H_BRANCH" || say "could not switch back to $H_REF"
        return 1
    fi
    # shellcheck disable=SC2086 # one image per word
    missing="$(missing_images $imgs)"
    if [[ -n "$missing" ]]; then
        say "images compose resolves at $T_REF are not on the Pi:$missing; switching back to $H_REF"
        restore_checkout "$H_REF" "$H_BRANCH" || say "could not switch back to $H_REF"
        return 1
    fi
    write_release "$T_REF" "$T_BRANCH" "$(current_tag_of "$imgs")" "$(tr '\n' ' ' <<<"$imgs" | sed 's/ $//')" \
        "$H_REF" "$H_BRANCH" "$H_TAG" "$H_IMGS" || {
        say "could not write $RELEASE_FILE"
        return 1
    }
    apply
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
    roll_back_to_previous || die "rollback to $T_REF failed or did not come up healthy (state failed)"
    say "rolled back to $T_REF"
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
