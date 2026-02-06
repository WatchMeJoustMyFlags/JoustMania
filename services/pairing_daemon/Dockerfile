# Multi-stage build for JoustMania Pairing Daemon Service
#
# Uses pre-built psmoveapi image for faster builds.
# Build the images first:
#   docker build -t ghcr.io/watchmejoustmyflags/joustmania/psmove-builder:latest images/psmove-builder/
#   docker build -t ghcr.io/watchmejoustmyflags/joustmania/builder:latest images/builder/
#
# Builder images can be overridden for CI/CD:
# --build-arg BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/builder:dev-refactor
# --build-arg PSMOVE_BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/psmove-builder:dev-refactor

# Builder images - override with --build-arg for CI/CD
ARG BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/builder:latest
ARG PSMOVE_BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/psmove-builder:latest

# =============================================================================
# Stage 1: PS Move API - use pre-built image
# =============================================================================
FROM ${PSMOVE_BUILDER_IMAGE} AS psmove-builder

# =============================================================================
# Stage 2: Build Python dependencies
# =============================================================================
FROM ${BUILDER_IMAGE} AS python-builder

# Install bluetooth/dbus build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libbluetooth-dev \
    libdbus-1-dev \
    libglib2.0-dev \
    libusb-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy workspace configuration
COPY pyproject.toml /app/

# Copy service dependencies
COPY services/pairing_daemon/pyproject.toml /app/services/pairing_daemon/

# Create minimal package structure for editable install (deps caching layer)
# Real source is copied in the runtime stage; stubs satisfy setuptools discovery
RUN mkdir -p services/pairing_daemon/psmove_pairing && \
    touch services/pairing_daemon/psmove_pairing/__init__.py && \
    touch services/pairing_daemon/server.py

# Install service dependencies (standalone - no proto/lib deps)
WORKDIR /app
RUN uv pip install --system --no-cache-dir -e services/pairing_daemon/

# =============================================================================
# Stage 3: Runtime image
# =============================================================================
FROM python:3.11-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    libbluetooth3 \
    libdbus-1-3 \
    libglib2.0-0 \
    libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from python-builder
COPY --from=python-builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=python-builder /usr/local/bin /usr/local/bin

# Copy psmove Python bindings from pre-built psmove-builder
COPY --from=psmove-builder /psmove/psmove.py /usr/local/lib/python3.11/site-packages/
COPY --from=psmove-builder /psmove/_psmove.so /usr/local/lib/python3.11/site-packages/
COPY --from=psmove-builder /psmove/libpsmoveapi.so /usr/local/lib/

# Copy psmove CLI tool for calibration
COPY --from=psmove-builder /psmove/psmove-cli /usr/local/bin/psmove

# Update library cache so _psmove.so can find libpsmoveapi.so
RUN ldconfig

# Copy application code
COPY --from=python-builder /app/pyproject.toml /app/
COPY services/pairing_daemon/ /app/services/pairing_daemon/

# Set Python path
ENV PYTHONPATH=/app/services/pairing_daemon
# Ensure Python uses bytecode cache for faster imports
ENV PYTHONDONTWRITEBYTECODE=0

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=psmove-pairing
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV OTEL_TRACES_EXPORTER=otlp
ENV OTEL_METRICS_EXPORTER=none
ENV OTEL_LOGS_EXPORTER=none

# Pairing daemon config
ENV PSMOVE_PATH=/usr/local/bin/psmove
ENV METRICS_PORT=8002

# Expose Prometheus metrics + healthz port
EXPOSE 8002

# Note: Pairing daemon needs privileged access for bluetooth and USB
# Run with: docker run --privileged or docker-compose privileged: true

# Health check using HTTP /healthz endpoint
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/healthz')" || exit 1

# Run the server
CMD ["python", "services/pairing_daemon/server.py"]
