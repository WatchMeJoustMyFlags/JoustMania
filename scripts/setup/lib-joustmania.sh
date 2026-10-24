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
