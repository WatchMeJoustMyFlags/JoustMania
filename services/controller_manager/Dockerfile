# Multi-stage build for JoustMania Controller Manager Service
#
# Build the builder image first:
#   docker build -t ghcr.io/watchmejoustmyflags/joustmania/builder:latest images/builder/
#
# Builder image can be overridden for CI/CD:
# --build-arg BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/builder:main

# Builder image - override with --build-arg for CI/CD
ARG BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/builder:latest

# =============================================================================
# Stage 1: Build Python dependencies
# =============================================================================
FROM ${BUILDER_IMAGE} AS python-builder

# Install additional bluetooth build dependencies (not in common builder)
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

# Copy proto package (shared protocol buffer contracts)
COPY proto/pyproject.toml /app/proto/
COPY proto/*.py /app/proto/
RUN python -m compileall -q proto/

# Copy lib package (shared utilities)
COPY lib/pyproject.toml /app/lib/
COPY lib/*.py /app/lib/
RUN python -m compileall -q lib/

# Copy service dependencies
COPY services/controller_manager/pyproject.toml /app/services/controller_manager/

# Install workspace packages and service dependencies
WORKDIR /app
RUN uv pip install --system -e proto/ -e lib/ -e services/controller_manager/

# =============================================================================
# Stage 2: Runtime image
# =============================================================================
FROM python:3.14-slim

# Install runtime dependencies (bluetooth for PS Move controllers)
# libhidapi-hidraw0: Required by bt_discovery for HID device enumeration
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    libbluetooth3 \
    libdbus-1-3 \
    libglib2.0-0 \
    libhidapi-hidraw0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from python-builder
COPY --from=python-builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=python-builder /usr/local/bin /usr/local/bin

# Copy grpc-health-probe binary
COPY --from=ghcr.io/grpc-ecosystem/grpc-health-probe:v0.4.52 /ko-app/grpc-health-probe /bin/grpc_health_probe

# Copy application code
COPY --from=python-builder /app/pyproject.toml /app/
COPY proto/ /app/proto/
COPY lib/ /app/lib/
COPY services/controller_manager/ /app/services/controller_manager/

# Set Python path
ENV PYTHONPATH=/app
# Ensure Python uses bytecode cache for faster imports (Phase 47)
ENV PYTHONDONTWRITEBYTECODE=0

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=controller-manager-service
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV OTEL_TRACES_EXPORTER=otlp
ENV OTEL_METRICS_EXPORTER=none
ENV OTEL_LOGS_EXPORTER=none

# Expose gRPC port
EXPOSE 50052

# Expose Prometheus metrics port (Phase 38)
EXPOSE 8000

# Note: Controller manager needs privileged access for bluetooth
# Run with: docker run --privileged or docker-compose privileged: true

# Health check using grpc-health-probe
HEALTHCHECK --interval=2s --timeout=2s --start-period=10s --retries=3 \
    CMD ["/bin/grpc_health_probe", "-addr=:50052"]

# Run the server
CMD ["python", "services/controller_manager/server.py"]
