# Multi-stage build for JoustMania Pairing Daemon Service
#
# Build the builder image first:
#   docker build -t ghcr.io/watchmejoustmyflags/joustmania/builder:latest images/builder/
#
# Builder image can be overridden for CI/CD:
# --build-arg BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/builder:dev-refactor

# Builder image - override with --build-arg for CI/CD
ARG BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/builder:latest

# =============================================================================
# Stage 1: Build Python dependencies
# =============================================================================
FROM ${BUILDER_IMAGE} AS python-builder

# Install bluetooth/dbus build dependencies
# libdbus-1-dev needed for dbus-python (legacy backend); dbus-fast is pure Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    libbluetooth-dev \
    libdbus-1-dev \
    libglib2.0-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy workspace configuration
COPY pyproject.toml /app/

# Copy lib package (OTEL metrics)
COPY lib/pyproject.toml /app/lib/
RUN mkdir -p lib && touch lib/__init__.py

# Copy proto package (gRPC stubs for rust-hid)
COPY proto/pyproject.toml /app/proto/
RUN mkdir -p proto && touch proto/__init__.py

# Copy service dependencies
COPY services/pairing_daemon/pyproject.toml /app/services/pairing_daemon/

# Create minimal package structure for editable install (deps caching layer)
# Real source is copied in the runtime stage; stubs satisfy setuptools discovery
RUN mkdir -p services/pairing_daemon/psmove_pairing && \
    touch services/pairing_daemon/psmove_pairing/__init__.py && \
    touch services/pairing_daemon/server.py

# Install lib + proto + service dependencies
WORKDIR /app
RUN uv pip install --system --no-cache-dir -e lib/ -e proto/ -e services/pairing_daemon/

# =============================================================================
# Stage 2: Runtime image
# =============================================================================
FROM python:3.11-slim

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    libbluetooth3 \
    libdbus-1-3 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from python-builder
COPY --from=python-builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=python-builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --from=python-builder /app/pyproject.toml /app/
COPY lib/ /app/lib/
COPY proto/ /app/proto/
COPY services/pairing_daemon/ /app/services/pairing_daemon/

# Set Python path (includes /app for lib imports)
ENV PYTHONPATH=/app:/app/services/pairing_daemon
# Ensure Python uses bytecode cache for faster imports
ENV PYTHONDONTWRITEBYTECODE=0

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=psmove-pairing
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV OTEL_TRACES_EXPORTER=otlp
ENV OTEL_LOGS_EXPORTER=none

# Pairing daemon config
ENV METRICS_PORT=8002

# Expose health check port
EXPOSE 8002

# Note: Pairing daemon needs privileged access for bluetooth and USB
# Run with: docker run --privileged or docker-compose privileged: true

# Health check using HTTP /healthz endpoint
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/healthz')" || exit 1

# Run the server
CMD ["python", "services/pairing_daemon/server.py"]
