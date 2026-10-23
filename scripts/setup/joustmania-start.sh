#!/bin/bash
# JoustMania startup script with optional auto-update
# Gracefully handles missing internet connection

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOUSTMANIA_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
# Start state lives in the unit's RuntimeDirectory (RUNTIME_DIRECTORY); an old
# unit has none, so fall back to /tmp. JOUSTMANIA_STATUS_DIR is the test override.
# Writing state is never fatal.
STATE_DIR="${JOUSTMANIA_STATUS_DIR:-${RUNTIME_DIRECTORY:-/tmp/joustmania}}"
STATE_FILE="$STATE_DIR/start.env"
START_RESULT="failed"

cd "$JOUSTMANIA_DIR"

# JOUSTMANIA_UNIT_REV comes from the installed unit (rev 2+). Without it the
# unit is outdated (it still has the teardown ExecStartPre and `down` at stop)
# or the script runs by hand: say so on every start, and never fail, so the
# old unit's Restart=on-failure cannot loop.
write_state() { # $1 = phase, $2 = result
    {
        mkdir -p "$STATE_DIR" &&
            printf 'phase=%s\nresult=%s\nhead=%s\nimage_tag=%s\nts=%s\n' \
                "$1" "$2" "${HEAD_SHORT:-unknown}" "${EFFECTIVE_IMAGE_TAG:-unknown}" "$(date -Iseconds)" \
                >"$STATE_FILE"
    } 2>/dev/null || true
}
on_exit() {
    local rc=$?
    write_state start "$([[ $rc -eq 0 ]] && echo "$START_RESULT" || echo failed)"
    if [[ $rc -ne 0 && -z "${JOUSTMANIA_UNIT_REV:-}" ]]; then
        echo "unit outdated: sudo $JOUSTMANIA_DIR/scripts/setup/install_autostart.sh (start failed, exit $rc; exiting 0 so the old unit does not loop)"
        exit 0
    fi
}
trap on_exit EXIT

# Every compose call goes through joustmania-compose.sh: docker-compose.yml
# only, never the dev override that publishes the mock control and gRPC ports
# to the venue network (#1319), with .env and, when present, the git-ignored
# .env.local (stage IMAGE_TAG and decision-backend API keys, #1488). It is
# resolved after the git pull below, so an updated wrapper applies at once.
COMPOSE_WRAPPER="$SCRIPT_DIR/joustmania-compose.sh"
compose() {
    "$COMPOSE_WRAPPER" "$@"
}

echo "=== JoustMania Startup ==="
echo "Working directory: $JOUSTMANIA_DIR"
if [[ -z "${JOUSTMANIA_UNIT_REV:-}" ]]; then
    echo "unit outdated or not run by the rev-2 unit (no JOUSTMANIA_UNIT_REV): sudo $JOUSTMANIA_DIR/scripts/setup/install_autostart.sh"
fi

mkdir -p "$STATE_DIR" 2>/dev/null || echo "state dir $STATE_DIR not writable; continuing"

# Docker may still be coming up: wait for it (the unit has Requires=docker.service)
for _ in $(seq 1 "${JOUSTMANIA_DOCKER_WAIT_S:-60}"); do
    docker info >/dev/null 2>&1 && break
    sleep 1
done

# Files to track for updates
TRACKED_FILES=(
    "scripts/setup/joustmania.service"
    "scripts/setup/joustmania-start.sh"
    "scripts/setup/joustmania-compose.sh"
    "scripts/setup/install_autostart.sh"
)

# Calculate checksums before git pull
declare -A CHECKSUMS_BEFORE
for file in "${TRACKED_FILES[@]}"; do
    if [[ -f "$file" ]]; then
        CHECKSUMS_BEFORE["$file"]=$(sha256sum "$file" 2>/dev/null | cut -d' ' -f1 || true)
    fi
done

# Attempt git pull (non-fatal)
echo "Checking for code updates..."
GIT_PULL_STATUS="success"
if git pull --ff-only 2>&1; then
    echo "Code updated successfully"
else
    echo "Could not update code (offline or conflict) - continuing with current version"
    GIT_PULL_STATUS="failed"
fi

# Check which files changed
CHANGED_FILES=()
for file in "${TRACKED_FILES[@]}"; do
    if [[ -f "$file" ]]; then
        CHECKSUM_AFTER=$(sha256sum "$file" 2>/dev/null | cut -d' ' -f1 || true)
        if [[ "${CHECKSUMS_BEFORE[$file]:-}" != "$CHECKSUM_AFTER" ]]; then
            CHANGED_FILES+=("$file")
            echo "Service file changed: $file"
        fi
    fi
done

HEAD_SHORT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
HEAD_TAG="$(git describe --tags --exact-match 2>/dev/null || echo none)"
# Effective IMAGE_TAG as compose resolves it (shell env, then .env.local, then
# .env; quotes, `export` and inline comments handled by compose): the tag of the
# game-coordinator image from `config --images`, which prints only image refs.
EFFECTIVE_IMAGE_TAG="unknown"
COMPOSE_IMAGES="$(compose config --images 2>/dev/null || true)"
GC_IMAGE="$(grep -m 1 '/game-coordinator-service[:@]' <<<"$COMPOSE_IMAGES" || true)"
GC_IMAGE="${GC_IMAGE%%@*}"
if [[ "${GC_IMAGE##*/}" == *:* ]]; then
    EFFECTIVE_IMAGE_TAG="${GC_IMAGE##*:}"
fi
echo "Version: commit $HEAD_SHORT, tag $HEAD_TAG, IMAGE_TAG $EFFECTIVE_IMAGE_TAG (git pull: $GIT_PULL_STATUS)"
write_state start starting

if [[ ${#CHANGED_FILES[@]} -gt 0 ]]; then
    echo "WARNING: Service files have changed. Run 'sudo ./scripts/setup/install_autostart.sh' to apply updates."
fi

# Say which env files apply, never their contents (they hold API keys)
if [[ -f .env.local ]]; then
    echo "Env files: .env + .env.local (.env.local wins)"
else
    echo "Env files: .env only (no .env.local: release IMAGE_TAG, no decision-backend keys)"
fi

# Attempt docker compose pull (non-fatal)
echo "Checking for image updates..."
if compose pull 2>&1; then
    echo "Images updated successfully"
else
    echo "Could not pull images (offline) - continuing with cached images"
fi

# Targeted cleanup only: a joustmania-* container that is not ours blocks the
# fixed container names, so remove it and say so. Compose itself says which
# containers are ours (`ps -a` resolves the project name from the shell env,
# .env and .env.local exactly as `up` does, and lists profile-inactive services
# too). A container whose compose working dir is this checkout is kept only when
# its project is one compose listed: one from this checkout under another project
# name (created before COMPOSE_PROJECT_NAME was set, or by a plain
# `docker compose up`) would block `up -d` with a name conflict, so it is
# foreign. If compose cannot list them, remove nothing. Our own containers are
# never touched here: compose keeps or recreates them (no blanket rm, prune or
# down; NFR-026).
echo "Checking for foreign joustmania containers..."
if OWN_LIST="$(compose ps -a --format '{{.Name}}|{{.Project}}' 2>/dev/null)"; then
    OWN_CONTAINERS="$(cut -d'|' -f1 <<<"$OWN_LIST")"
    OWN_PROJECTS="$(cut -s -d'|' -f2 <<<"$OWN_LIST" | sort -u)"
    CONTAINERS="$(docker ps -a --filter "name=joustmania-" --format '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
    while IFS='|' read -r cname cproject cworkdir; do
        [[ -n "$cname" ]] || continue
        grep -qxF -- "$cname" <<<"$OWN_CONTAINERS" && continue
        if [[ "$cworkdir" == "$JOUSTMANIA_DIR" && -n "$cproject" ]] && grep -qxF -- "$cproject" <<<"$OWN_PROJECTS"; then
            continue
        fi
        echo "Removing foreign container $cname (project '${cproject:-none}')"
        docker rm -f "$cname" >/dev/null 2>&1 || true
    done <<<"$CONTAINERS"
else
    echo "Could not list this project's containers; removing none"
fi

# Set all audio devices to max volume
echo "Setting audio volume..."
for card in "${JOUSTMANIA_ASOUND_DIR:-/proc/asound}"/card[0-9]*; do
  [[ -e "$card" ]] || continue
  card_num=$(basename "$card" | sed 's/card//')
  # pipefail + errexit: a card without controls or a failing amixer is fine
  { amixer -c "$card_num" scontrols 2>/dev/null || true; } | sed -e "s/^Simple mixer control //" | while read -r ctrl; do
    amixer -c "$card_num" set "$ctrl" 100% unmute 2>/dev/null || true
  done || true
done
echo "Audio volume configured"

# Real controllers are read through rust-hid, the default Bluetooth backend
# (#1371); it is part of the default compose services. python-hid is opt-in and
# needs all three of:
#   1. COMPOSE_PROFILES=python-hid, set in a systemd drop-in (not in .env: .env
#      is tracked, and editing it breaks the `git pull --ff-only` above):
#        sudo systemctl edit joustmania
#          [Service]
#          Environment=COMPOSE_PROFILES=python-hid
#   2. backend=python_rust in services/flagd/controller.json (loads the python
#      adapter next to rust; read once, restart controller-manager), AND
#   3. bluetooth_backend=python (routes serials and pairing to python-hid; its
#      default "rust" keeps every controller on rust-hid).
#
# Upgrade step (#1402 I1): Pis that ran python-hid before #1371 keep a
# joustmania-python-hid container (restart: unless-stopped), and `up -d` never
# removes a container whose profile is inactive. Remove it explicitly whenever
# the python-hid profile is not active. Idempotent: no-op if it is absent.
# The services go through a variable: `config | grep -q` under pipefail dies of
# SIGPIPE when the list is long and grep exits early (audit F4).
COMPOSE_SERVICES="$(compose config --services 2>/dev/null || true)"
if ! grep -qx python-hid <<<"$COMPOSE_SERVICES"; then
    docker rm -f joustmania-python-hid >/dev/null 2>&1 || true
fi

# Start services (this must succeed). Not exec: the EXIT trap records the result.
echo "Starting JoustMania (Bluetooth backend: rust-hid${COMPOSE_PROFILES:+, profiles: ${COMPOSE_PROFILES}})..."
compose up -d --remove-orphans
START_RESULT="ok"
