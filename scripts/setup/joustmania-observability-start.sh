#!/bin/bash
# JoustMania Observability startup script
# Manages the observability stack (Jaeger, Grafana, Prometheus, etc.)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOUSTMANIA_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"

cd "$JOUSTMANIA_DIR"

echo "=== JoustMania Observability Startup ==="

# Ensure shared network exists
docker network inspect joustmania-network >/dev/null 2>&1 || docker network create joustmania-network

# Clean up stale containers
echo "Cleaning up stale observability containers..."
docker ps -aq \
  --filter "name=joustmania-jaeger" \
  --filter "name=joustmania-otel-collector" \
  --filter "name=joustmania-prometheus" \
  --filter "name=joustmania-grafana" \
  --filter "name=joustmania-victoria-metrics" \
  --filter "name=joustmania-loki" \
  --filter "name=joustmania-node-exporter" \
  --filter "name=joustmania-grafana-live-publisher" \
  --filter "name=joustmania-pyroscope" \
  | xargs -r docker rm -f 2>/dev/null || true

# Pull images (non-fatal)
echo "Checking for observability image updates..."
docker compose -f docker-compose.observability.yml pull 2>&1 || echo "Could not pull images - continuing with cached"

# Clean existing
echo "Cleaning up..."
docker compose -f docker-compose.observability.yml down --remove-orphans || true

# Start
echo "Starting observability stack..."
exec docker compose -f docker-compose.observability.yml up -d
