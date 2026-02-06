#!/bin/bash
#
# Install PS Move Pairing Daemon (binary distribution)
#
# Downloads a pre-built binary from GitHub Releases and installs it as a
# systemd service. No Python or pip required on the host.
#
# Usage:
#   sudo ./scripts/pairing-daemon/install.sh                  # Download latest release
#   sudo ./scripts/pairing-daemon/install.sh --local ./binary  # Use a local binary
#   sudo ./scripts/pairing-daemon/install.sh --legacy          # Use venv-based install
#
# For the legacy Python venv install, see install-legacy.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/joustmania/bin"
GITHUB_REPO="WatchMeJoustMyFlags/JoustMania"
BINARY_NAME="psmove-pairing-daemon"

# --- Helpers ---

die() { echo "ERROR: $*" >&2; exit 1; }

detect_arch() {
    local machine
    machine="$(uname -m)"
    case "$machine" in
        x86_64)  echo "amd64" ;;
        aarch64) echo "arm64" ;;
        armv7l)  die "armv7l (32-bit ARM) is not supported. Use Raspberry Pi OS 64-bit." ;;
        *)       die "Unsupported architecture: $machine" ;;
    esac
}

download_binary() {
    local arch="$1"
    local dest="$2"
    local asset_name="${BINARY_NAME}-${arch}"
    local release_url

    echo "  Finding latest pairing-daemon release..."
    # Use the GitHub Releases API to find the download URL.
    # Parses JSON with grep/sed to avoid requiring python3 or jq on the host.
    release_url=$(curl -fsSL \
        "https://api.github.com/repos/${GITHUB_REPO}/releases?per_page=10" \
        | grep -o "\"browser_download_url\"[[:space:]]*:[[:space:]]*\"[^\"]*${asset_name}\"" \
        | head -1 \
        | grep -o 'https://[^"]*') || true

    if [ -z "$release_url" ]; then
        die "No release found for '${asset_name}'. Check https://github.com/${GITHUB_REPO}/releases"
    fi

    echo "  Downloading ${asset_name}..."
    curl -fsSL -o "$dest" "$release_url"
    chmod +x "$dest"
}

# --- Parse arguments ---

LOCAL_BINARY=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local)
            LOCAL_BINARY="$2"
            shift 2
            ;;
        --legacy)
            echo "Falling back to legacy venv-based install..."
            exec "$SCRIPT_DIR/install-legacy.sh"
            ;;
        -h|--help)
            echo "Usage: $0 [--local BINARY_PATH] [--legacy]"
            echo ""
            echo "Options:"
            echo "  --local PATH   Install from a local binary instead of downloading"
            echo "  --legacy       Use the legacy Python venv-based installer"
            exit 0
            ;;
        *)
            die "Unknown option: $1"
            ;;
    esac
done

# --- Require root ---
if [[ $EUID -ne 0 ]]; then
    die "This script must be run with sudo"
fi

echo "Installing PS Move Pairing Daemon..."

# --- Install udev rules ---
echo "  Installing udev rules..."
cat > /etc/udev/rules.d/99-psmove.rules << 'EOF'
# PS Move Motion Controller (USB access for pairing)
SUBSYSTEM=="usb", ATTR{idVendor}=="054c", ATTR{idProduct}=="03d5", MODE="0666"
SUBSYSTEM=="usb", ATTR{idVendor}=="054c", ATTR{idProduct}=="042f", MODE="0666"
# PS Move Navigation Controller
SUBSYSTEM=="usb", ATTR{idVendor}=="054c", ATTR{idProduct}=="03d4", MODE="0666"
EOF
udevadm control --reload-rules
udevadm trigger

# --- Install binary ---
echo "  Creating installation directory..."
mkdir -p "$INSTALL_DIR"

if [ -n "$LOCAL_BINARY" ]; then
    echo "  Installing from local binary: $LOCAL_BINARY"
    [ -f "$LOCAL_BINARY" ] || die "Local binary not found: $LOCAL_BINARY"
    cp "$LOCAL_BINARY" "$INSTALL_DIR/$BINARY_NAME"
    chmod +x "$INSTALL_DIR/$BINARY_NAME"
else
    ARCH="$(detect_arch)"
    download_binary "$ARCH" "$INSTALL_DIR/$BINARY_NAME"
fi

echo "  Binary installed: $INSTALL_DIR/$BINARY_NAME"

# --- Install systemd service ---
echo "  Installing systemd service..."
cp "$SCRIPT_DIR/psmove-pairing.service" /etc/systemd/system/

# --- Reload and start ---
echo "  Reloading systemd..."
systemctl daemon-reload

echo "  Enabling service..."
systemctl enable psmove-pairing.service

echo "  Starting service..."
systemctl restart psmove-pairing.service

echo ""
echo "PS Move Pairing Daemon installed and running!"
echo ""
echo "Commands:"
echo "  View logs:    journalctl -u psmove-pairing -f"
echo "  Status:       systemctl status psmove-pairing"
echo "  Restart:      sudo systemctl restart psmove-pairing"
echo "  Stop:         sudo systemctl stop psmove-pairing"
echo "  Metrics:      curl http://localhost:8002/metrics"
echo ""
echo "To pair a controller:"
echo "  1. Plug in PS Move via USB"
echo "  2. Wait for white flash (success)"
echo "  3. Unplug USB and press PS button"
echo ""
