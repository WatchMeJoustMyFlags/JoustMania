#!/bin/bash
# Shared helpers for the Pi scripts (UC-060, #2027). Sourced, never run.
# Used by joustmania-compose.sh (every compose call), joustmania-start.sh and
# joustmania-upgrade.sh, so the pin and the image tag are derived in one place.

# Game services: the ones an upgrade waits for before it calls the new version
# healthy (UC-060 main step 3, A2 step 3). Observability, the agent, the
# dashboard, connect-proxy and envoy only make the state degraded (A1, BR-002).
# Only the services the active profiles enable are checked (python-hid is opt-in).
JOUSTMANIA_GAME_SERVICES=(flag-service flagd controller-manager rust-hid python-hid pairing-daemon game-coordinator menu audio)

# joustmania_pin_image_tag <checkout dir>
# Prints the image tag the checkout pins (UC-060 BR-003, C-040) and returns 0,
# or prints nothing and returns 1 when the checkout is not pinned (HEAD on a
# branch) or git cannot read it. Pinned = detached HEAD:
#   - an exact release tag joustmania-vX.Y.Z at HEAD -> X.Y.Z (the highest one
#     when several point at HEAD);
#   - otherwise the full 40-character commit sha (CI tags main builds with it).
joustmania_pin_image_tag() {
    local dir="$1" sha tags tag
    git -C "$dir" symbolic-ref -q HEAD >/dev/null 2>&1 && return 1
    sha="$(git -C "$dir" rev-parse --verify -q 'HEAD^{commit}' 2>/dev/null)" || return 1
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 1
    tags="$(git -C "$dir" tag --points-at "$sha" --list 'joustmania-v*' 2>/dev/null || true)"
    tag="$(grep -E '^joustmania-v[0-9]+\.[0-9]+\.[0-9]+$' <<<"$tags" | sed 's/^joustmania-v//' | sort -V | tail -n 1 || true)"
    printf '%s\n' "${tag:-$sha}"
}

# joustmania_env_local_sets <file> <key>: true when the env file assigns <key>
# (plain or `export KEY=`). Never prints the value (the file holds API keys).
joustmania_env_local_sets() {
    [[ -f "$1" ]] && grep -qE "^[[:space:]]*(export[[:space:]]+)?$2[[:space:]]*=" "$1"
}

# Second start phase (UC-060 main step 4, BR-004): the observability backends,
# the collector and the agent. The game never waits for them and none of them
# can fail the unit (a missing image or a failed start is `degraded`, BR-002).
# Every other active service (the game services above plus connect-proxy,
# dashboard, envoy and the opt-in operator-pad) starts in the first phase.
JOUSTMANIA_OBS_SERVICES=(jaeger loki victoria-metrics otel-collector grafana agent)

# joustmania_in_list <word> <list...>: true when <word> is one of <list>
joustmania_in_list() {
    local word="$1" item
    shift
    for item in "$@"; do
        [[ "$item" == "$word" ]] && return 0
    done
    return 1
}

# joustmania_service_graph: reads `docker compose config` (canonical YAML) on
# stdin and prints `service|image|dep1,dep2` for every active service (deps =
# its depends_on keys). The full config holds interpolated secrets: pipe it
# straight in, never log or store it.
joustmania_service_graph() {
    awk '
        function flush() { if (svc != "") print svc "|" img "|" deps; svc = ""; img = ""; deps = "" }
        /^services:$/ { in_services = 1; next }
        in_services && /^[^ ]/ { flush(); in_services = 0 }
        !in_services { next }
        /^  [^ ][^ ]*:$/ { flush(); svc = $1; sub(/:$/, "", svc); in_deps = 0; next }
        /^    [^ ]/ { in_deps = ($0 ~ /^    depends_on:$/) }
        /^    image: / { img = $2; sub(/@.*$/, "", img) }
        in_deps && /^      [^ ][^ ]*:$/ { d = $1; sub(/:$/, "", d); deps = deps (deps == "" ? "" : ",") d }
        in_deps && /^      - / { deps = deps (deps == "" ? "" : ",") $2 }
        END { flush() }
    '
}

# joustmania_blocked <graph> <unavailable services...>: prints the services
# (one per line) whose depends_on chain reaches an unavailable one. `up`
# starts a named service's dependencies and aborts as a whole on a missing
# image, so these must not be named (UC-060 BR-002).
joustmania_blocked() {
    local graph="$1" svc img deps dep changed=1
    shift
    local -a bad=("$@") blocked=()
    while ((changed)); do
        changed=0
        while IFS='|' read -r svc img deps; do
            [[ -n "$svc" && -n "$deps" ]] || continue
            joustmania_in_list "$svc" "${bad[@]}" && continue
            for dep in ${deps//,/ }; do
                if joustmania_in_list "$dep" "${bad[@]}"; then
                    bad+=("$svc")
                    blocked+=("$svc")
                    changed=1
                    break
                fi
            done
        done <<<"$graph"
    done
    : "$img"
    ((${#blocked[@]})) && printf '%s\n' "${blocked[@]}"
    return 0
}

# joustmania_pin_ref <checkout dir>: the ref the upgrade command takes for the
# version checked out (the joustmania-vX.Y.Z tag at HEAD, else the full sha),
# for the recovery hint. Prints nothing when git cannot read HEAD.
joustmania_pin_ref() {
    local tag
    tag="$(joustmania_pin_image_tag "$1")" || tag=""
    if [[ "$tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        printf 'joustmania-v%s\n' "$tag"
    else
        git -C "$1" rev-parse --verify -q 'HEAD^{commit}' 2>/dev/null || true
    fi
}

# joustmania_missing_hint <checkout dir>: the recovery for images missing on
# the Pi. Boot never pulls (UC-060 BR-001); the upgrade command pulls the
# pinned version's images while online, and re-running it for the version
# already checked out pulls only what is missing (A2).
joustmania_missing_hint() {
    local ref
    ref="$(joustmania_pin_ref "$1")"
    printf 'when online, run: %s/scripts/setup/joustmania-upgrade.sh %s\n' "$1" "${ref:-<joustmania-vX.Y.Z | sha>}"
}

# joustmania_hotspot_hint <checkout dir> <old commit> <new commit>: one line when
# the stage hotspot is installed (UC-061) and its files differ between the two
# commits. The installed root-owned copies never change with the checkout
# (UC-061 BR-006), so the operator re-runs the setup to take the new ones.
# JOUSTMANIA_HOTSPOT_INSTALLED overrides the installed helper's path (tests).
joustmania_hotspot_hint() {
    local installed="${JOUSTMANIA_HOTSPOT_INSTALLED:-/usr/local/lib/joustmania/joustmania-hotspot}"
    [[ -e "$installed" ]] || return 0
    git -C "$1" diff --quiet "$2" "$3" -- scripts/setup/joustmania-hotspot.sh \
        scripts/setup/joustmania-hotspot.service scripts/setup/joustmania-hotspot.timer \
        scripts/setup/90-joustmania-hotspot scripts/setup/joustmania-hotspot.nmconnection.template \
        2>/dev/null && return 0
    printf 'hotspot files changed: run sudo %s/scripts/setup/install_hotspot.sh (UC-061; the password is asked again)\n' "$1"
}
