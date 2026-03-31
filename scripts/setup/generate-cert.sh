#!/bin/bash
# Generate self-signed TLS certificate for JoustMania
#
# Required for DeviceMotion API access on mobile phones (HTTPS-only).
# Generates a certificate valid for 10 years.

set -e

CERT_DIR="${CERT_DIR:-/etc/joustmania}"
CERT_FILE="$CERT_DIR/tls.crt"
KEY_FILE="$CERT_DIR/tls.key"

# Skip if cert already exists
if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
    echo "TLS certificate already exists at $CERT_DIR"
    exit 0
fi

echo "Generating self-signed TLS certificate..."

mkdir -p "$CERT_DIR"

# Get hostname and IP for SAN
HOSTNAME=$(hostname)
# Try to detect local IP address
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "")

# Build SAN extension
SAN="DNS:joustmania,DNS:localhost,DNS:$HOSTNAME"
if [[ -n "$LOCAL_IP" ]]; then
    SAN="$SAN,IP:$LOCAL_IP"
fi
SAN="$SAN,IP:127.0.0.1"

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -days 3650 \
    -subj "/CN=joustmania" \
    -addext "subjectAltName=$SAN"

chmod 644 "$CERT_FILE"
chmod 600 "$KEY_FILE"

echo "TLS certificate generated:"
echo "  Certificate: $CERT_FILE"
echo "  Private key: $KEY_FILE"
echo "  SAN: $SAN"
echo ""
echo "Phones will need to accept the self-signed cert warning."
