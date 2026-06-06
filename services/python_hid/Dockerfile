# Multi-stage build for JoustMania Python HID Service
#
# Standalone gRPC service implementing ControllerIOService from psmove_hid.proto.
# Same interface as rust-hid for apples-to-apples canary comparison.

# Builder image - override with --build-arg for CI/CD
ARG BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/builder:latest

# =============================================================================
# Stage 1: Build Python dependencies
# =============================================================================
FROM ${BUILDER_IMAGE} AS python-builder

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
COPY services/python_hid/pyproject.toml /app/services/python_hid/

# Install workspace packages and service dependencies
WORKDIR /app
RUN uv pip install --system -e proto/ -e lib/ -e services/python_hid/

# =============================================================================
# Stage 2: Runtime image
# =============================================================================
FROM python:3.14-slim

# Install runtime dependencies
# libhidapi-hidraw0: Required for hidapi backend (HID over hidraw)
RUN apt-get update && apt-get install -y --no-install-recommends \
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
COPY services/python_hid/ /app/services/python_hid/

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=0

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=python-hid
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV OTEL_TRACES_EXPORTER=otlp
ENV OTEL_METRICS_EXPORTER=none
ENV OTEL_LOGS_EXPORTER=none

# Expose gRPC port
EXPOSE 50059

# Health check using grpc-health-probe
HEALTHCHECK --interval=2s --timeout=2s --start-period=5s --retries=3 \
    CMD ["/bin/grpc_health_probe", "-addr=:50059"]

# Run the server
CMD ["python", "services/python_hid/server.py"]
