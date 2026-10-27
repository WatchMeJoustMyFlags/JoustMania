#!/bin/bash
# Uninstall JoustMania systemd service

set -e

SERVICE_NAME="joustmania.service"
SYSTEMD_DIR="/etc/systemd/system"

echo "Uninstalling JoustMania autostart service..."

# Check if running as root
if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: This script must be run as root (use sudo)" >&2
    exit 1
fi

# Stop the service if running
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo "Stopping $SERVICE_NAME..."
    systemctl stop "$SERVICE_NAME"
fi

# Disable the service
if systemctl is-enabled --quiet "$SERVICE_NAME"; then
    echo "Disabling $SERVICE_NAME..."
    systemctl disable "$SERVICE_NAME"
fi

# Put the board's own ACT LED trigger back (a failed unit leaves it blinking)
# and remove the root-owned LED helper (UC-060 BR-005)
LED_HELPER_DIR="/usr/local/lib/joustmania"
if [[ -x "$LED_HELPER_DIR/joustmania-led" ]]; then
    "$LED_HELPER_DIR/joustmania-led" restore || true
    rm -f "$LED_HELPER_DIR/joustmania-led"
    rmdir "$LED_HELPER_DIR" 2>/dev/null || true
fi

# Remove service file
if [[ -f "$SYSTEMD_DIR/$SERVICE_NAME" ]]; then
    echo "Removing service file from $SYSTEMD_DIR/$SERVICE_NAME"
    rm "$SYSTEMD_DIR/$SERVICE_NAME"
fi

# Reload systemd daemon
echo "Reloading systemd daemon..."
systemctl daemon-reload
systemctl reset-failed

echo ""
echo "✓ JoustMania autostart service uninstalled successfully!"
echo ""
echo "JoustMania will no longer start automatically on boot."
if [[ -x /usr/local/lib/joustmania/joustmania-hotspot ]]; then
    echo "The stage hotspot fallback (UC-061) is separate and stays installed; remove it with:"
    echo "  sudo /usr/local/lib/joustmania/joustmania-hotspot remove"
fi
echo "You can manually start it with: ./scripts/setup/joustmania-start.sh (or ./scripts/setup/joustmania-compose.sh up -d)"
