#!/bin/bash
# JoustMania startup script with optional auto-update
# Gracefully handles missing internet connection

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOUSTMANIA_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
STATUS_DIR="${JOUSTMANIA_STATUS_DIR:-/tmp/joustmania}"
STATUS_FILE="$STATUS_DIR/update-status.json"

cd "$JOUSTMANIA_DIR"

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

# Create status directory
mkdir -p "$STATUS_DIR"

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
        CHECKSUMS_BEFORE["$file"]=$(sha256sum "$file" 2>/dev/null | cut -d' ' -f1)
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
        CHECKSUM_AFTER=$(sha256sum "$file" 2>/dev/null | cut -d' ' -f1)
        if [[ "${CHECKSUMS_BEFORE[$file]}" != "$CHECKSUM_AFTER" ]]; then
            CHANGED_FILES+=("$file")
            echo "Service file changed: $file"
        fi
    fi
done

# Write status file for observability
CHANGED_FILES_JSON=$(printf '%s\n' "${CHANGED_FILES[@]}" | jq -R . | jq -s .)
cat > "$STATUS_FILE" << EOF
{
    "timestamp": "$(date -Iseconds)",
    "git_pull_status": "$GIT_PULL_STATUS",
    "service_files_changed": ${CHANGED_FILES_JSON:-[]},
    "update_pending": $([[ ${#CHANGED_FILES[@]} -gt 0 ]] && echo "true" || echo "false")
}
EOF

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

# Force-remove any stale joustmania containers from previous unclean shutdown
echo "Cleaning up stale containers..."
docker ps -aq --filter "name=joustmania-" | xargs -r docker rm -f 2>/dev/null || true
docker container prune -f 2>/dev/null || true

# Normal compose cleanup
echo "Cleaning up..."
compose down --remove-orphans || true

# Set all audio devices to max volume
echo "Setting audio volume..."
for card in /proc/asound/card[0-9]*; do
  card_num=$(basename "$card" | sed 's/card//')
  amixer -c "$card_num" scontrols 2>/dev/null | sed -e "s/^Simple mixer control //" | while read ctrl; do
    amixer -c "$card_num" set "$ctrl" 100% unmute 2>/dev/null
  done
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
# the python-hid profile is not active. Idempotent: no-op if it is absent. The
# blanket joustmania-* cleanup above covers it today; this keeps the upgrade
# step explicit should that cleanup ever be narrowed.
if ! compose config --services 2>/dev/null | grep -qx python-hid; then
    docker rm -f joustmania-python-hid >/dev/null 2>&1 || true
fi

# Start services (this must succeed)
echo "Starting JoustMania (Bluetooth backend: rust-hid${COMPOSE_PROFILES:+, profiles: ${COMPOSE_PROFILES}})..."
exec "$COMPOSE_WRAPPER" up -d
