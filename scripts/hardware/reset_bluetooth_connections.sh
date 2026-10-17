#!/usr/bin/env bash
# Remove PS Move Bluetooth registrations (live BlueZ devices and saved-only
# registrations of adapters BlueZ does not present). Other Bluetooth devices
# (keyboards, headsets, ...) are left alone. No reboot.
#
# Usage: scripts/hardware/reset_bluetooth_connections.sh [--dry-run]
#
# Runs psmove_pairing.registrations inside the pairing-daemon container, which
# has the system D-Bus and /var/lib/bluetooth mounted. After a real run it
# restarts the pairing daemon: the running daemon remembers controllers it
# already verified this session and would otherwise skip re-pairing them over
# USB. Exit code is non-zero if any removal failed. Afterwards, re-pair the
# controllers over USB.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

DRY_RUN=0
for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=1
done

CMD=(python -m psmove_pairing.registrations "$@")
# Compose as the Pi runs it (no dev override, #1319; .env + .env.local, #1488)
COMPOSE=("$REPO_ROOT/scripts/setup/joustmania-compose.sh")

status=0
if "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx pairing-daemon; then
    "${COMPOSE[@]}" exec -T pairing-daemon "${CMD[@]}" || status=$?
    if [[ "$DRY_RUN" -eq 0 ]]; then
        echo "Restarting pairing-daemon so it re-pairs known controllers over USB..."
        "${COMPOSE[@]}" restart pairing-daemon
    fi
else
    echo "pairing-daemon is not running; starting a one-off container" >&2
    "${COMPOSE[@]}" run --rm --no-deps -T pairing-daemon "${CMD[@]}" || status=$?
fi

exit "$status"
