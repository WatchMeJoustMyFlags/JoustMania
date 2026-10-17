#!/usr/bin/env bash
# Remove PS Move Bluetooth registrations (live BlueZ devices and saved-only
# registrations of unplugged adapters). Other Bluetooth devices (keyboards,
# headsets, ...) are left alone. No reboot.
#
# Usage: scripts/hardware/reset_bluetooth_connections.sh [--dry-run]
#
# Runs psmove_pairing.registrations inside the pairing-daemon container, which
# has the system D-Bus and /var/lib/bluetooth mounted. Afterwards, re-pair the
# controllers over USB.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

CMD=(python -m psmove_pairing.registrations "$@")

if docker compose ps --status running --services 2>/dev/null | grep -qx pairing-daemon; then
    docker compose exec -T pairing-daemon "${CMD[@]}"
else
    echo "pairing-daemon is not running; starting a one-off container" >&2
    docker compose run --rm --no-deps -T pairing-daemon "${CMD[@]}"
fi
