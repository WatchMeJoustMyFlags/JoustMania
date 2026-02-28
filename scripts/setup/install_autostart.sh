#!/bin/bash
# Install JoustMania systemd services for autostart on boot
# Installs both the game stack and observability stack as independent services.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="/etc/systemd/system"

# Service definitions: name -> source file
declare -A SERVICES=(
    ["joustmania.service"]="$SCRIPT_DIR/joustmania.service"
    ["joustmania-observability.service"]="$SCRIPT_DIR/joustmania-observability.service"
)

echo "Installing JoustMania autostart services..."

# Check if running as root
if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: This script must be run as root (use sudo)" >&2
    exit 1
fi

# Get the actual user (not root when using sudo)
ACTUAL_USER="${SUDO_USER:-$USER}"
ACTUAL_HOME=$(eval echo "~$ACTUAL_USER")

echo "Configuring services for user: $ACTUAL_USER"
echo "Home directory: $ACTUAL_HOME"

# Install each service file
for SERVICE_NAME in "${!SERVICES[@]}"; do
    SERVICE_FILE="${SERVICES[$SERVICE_NAME]}"

    if [[ ! -f "$SERVICE_FILE" ]]; then
        echo "WARNING: Service file not found: $SERVICE_FILE (skipping)"
        continue
    fi

    # Create temporary service file with correct paths
    TMP_SERVICE=$(mktemp)
    sed "s|/home/pi|$ACTUAL_HOME|g" "$SERVICE_FILE" | \
    sed "s|User=pi|User=$ACTUAL_USER|g" | \
    sed "s|Group=pi|Group=$ACTUAL_USER|g" > "$TMP_SERVICE"

    # Copy service file to systemd directory
    echo "Installing $SERVICE_NAME to $SYSTEMD_DIR/"
    cp "$TMP_SERVICE" "$SYSTEMD_DIR/$SERVICE_NAME"
    rm "$TMP_SERVICE"

    # Set proper permissions
    chmod 644 "$SYSTEMD_DIR/$SERVICE_NAME"
done

# Reload systemd daemon
echo "Reloading systemd daemon..."
systemctl daemon-reload

# Enable all services
for SERVICE_NAME in "${!SERVICES[@]}"; do
    if [[ -f "$SYSTEMD_DIR/$SERVICE_NAME" ]]; then
        echo "Enabling $SERVICE_NAME..."
        systemctl enable "$SERVICE_NAME"
    fi
done

# Show service status
echo ""
echo "JoustMania autostart services installed successfully!"
echo ""
echo "Available commands:"
echo "  sudo systemctl start joustmania                    - Start game stack"
echo "  sudo systemctl start joustmania-observability      - Start observability stack"
echo "  sudo systemctl stop joustmania                     - Stop game stack"
echo "  sudo systemctl stop joustmania-observability       - Stop observability stack"
echo "  sudo systemctl restart joustmania                  - Restart game stack"
echo "  sudo systemctl restart joustmania-observability    - Restart observability stack"
echo "  sudo systemctl status joustmania                   - Check game status"
echo "  sudo systemctl status joustmania-observability     - Check observability status"
echo "  sudo journalctl -u joustmania -f                   - View game logs"
echo "  sudo journalctl -u joustmania-observability -f     - View observability logs"
echo ""
echo "Both stacks will start automatically on boot."
echo "The observability stack starts first, then the game stack."
