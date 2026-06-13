# Multi-stage build for JoustMania Controller Manager Service
#
# #914: builds on a generic python:3.14-slim base (no shared builder image).
# All deps ship wheels (dbus-fast is pure Python, hidapi has cp314 aarch64
# wheels), so no compiler/toolchain is needed in the build stage.

# =============================================================================
# Stage 1: Build Python dependencies
# =============================================================================
FROM python:3.14-slim AS python-builder

# uv: fast Python package installer
RUN pip install --no-cache-dir uv==0.5.11

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

# Dependency fingerprint baked at build time so the integration fast path can
# detect a stale :latest before mounting current source into it (#925).
# Computed by scripts/ci/deps-fingerprint.sh; read back by
# scripts/ci/check-image-freshness.sh. "dev" when built outside CI.
ARG DEPS_FINGERPRINT=dev
LABEL org.joustmania.deps-fingerprint="${DEPS_FINGERPRINT}"

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
COPY --from=python-builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
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
