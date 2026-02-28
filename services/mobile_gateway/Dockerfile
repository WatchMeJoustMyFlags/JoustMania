# Mobile Gateway Service — serves phone UI and bridges WebSocket to gRPC
#
# Lightweight aiohttp service with no native dependencies.
# Phase 75: Builder images can be overridden for CI/CD

ARG BUILDER_IMAGE=ghcr.io/watchmejoustmyflags/joustmania/builder:latest

# =============================================================================
# Stage 1: Build Python dependencies
# =============================================================================
FROM ${BUILDER_IMAGE} AS builder

WORKDIR /app

# Copy workspace configuration
COPY pyproject.toml /app/

# Copy proto package
COPY proto/pyproject.toml /app/proto/
COPY proto/*.py /app/proto/
RUN python -m compileall -q proto/

# Copy lib package
COPY lib/pyproject.toml /app/lib/
COPY lib/*.py /app/lib/
RUN python -m compileall -q lib/

# Copy service dependencies
COPY services/mobile_gateway/pyproject.toml /app/services/mobile_gateway/

# Install workspace packages and service dependencies
WORKDIR /app
RUN uv pip install --system -e proto/ -e lib/ -e services/mobile_gateway/

# =============================================================================
# Stage 2: Runtime image
# =============================================================================
FROM python:3.11-slim

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY --from=builder /app/pyproject.toml /app/
COPY proto/ /app/proto/
COPY lib/ /app/lib/
COPY services/mobile_gateway/ /app/services/mobile_gateway/

# Set Python path
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=0

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=mobile-gateway-service
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV OTEL_TRACES_EXPORTER=otlp
ENV OTEL_METRICS_EXPORTER=none
ENV OTEL_LOGS_EXPORTER=none

# Expose HTTP/WebSocket port
EXPOSE 8070

# Health check via aiohttp
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8070/mobile/')"]

# Run the server
CMD ["python", "services/mobile_gateway/server.py"]
