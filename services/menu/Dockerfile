# Multi-stage build for JoustMania Menu Service
#
# #914: builds on a generic python:3.14-slim base (no shared builder image).
# All deps ship wheels, so no compiler/toolchain is needed in the build stage.

# Build stage - generic slim base + uv (deps all ship wheels)
FROM python:3.14-slim AS builder

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
COPY services/menu/pyproject.toml /app/services/menu/

# Install workspace packages and service dependencies
WORKDIR /app
RUN uv pip install --system -e proto/ -e lib/ -e services/menu/

# Runtime stage - Chainguard distroless-style image, minimal size and attack surface
#
# Python 3.14: the gcr.io/distroless/python3-debian12 image ships Python 3.11,
# which is ABI-incompatible with packages built against the 3.14 builder.
# Chainguard's python image tracks 3.14 (Wolfi/glibc). Digest-pinned because the
# free tier only publishes the moving `latest` tag (no version-specific tags).
FROM cgr.dev/chainguard/python:latest@sha256:678e879909418cd070927d0ba1ed018be98d43929db2457c37b9b9764703678c

# Dependency fingerprint baked at build time so the integration fast path can
# detect a stale :latest before mounting current source into it (#925).
# Computed by scripts/ci/deps-fingerprint.sh; read back by
# scripts/ci/check-image-freshness.sh. "dev" when built outside CI.
ARG DEPS_FINGERPRINT=dev
LABEL org.joustmania.deps-fingerprint="${DEPS_FINGERPRINT}"

WORKDIR /app

# Copy grpc-health-probe for health checks
# Copy grpc-health-probe from official multi-arch image
COPY --from=ghcr.io/grpc-ecosystem/grpc-health-probe:v0.4.52 /ko-app/grpc-health-probe /bin/grpc_health_probe

# Copy installed packages from builder onto a Chainguard sys.path entry
# (chainguard python: sys.path includes /usr/lib/python3.14)
COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/lib/python3.14/

# Copy application code
COPY --from=builder /app/pyproject.toml /app/
COPY proto/ /app/proto/
COPY lib/ /app/lib/
COPY services/menu/ /app/services/menu/

# Set Python path for application code
ENV PYTHONPATH=/app
# Ensure Python uses bytecode cache for faster imports (Phase 47)
ENV PYTHONDONTWRITEBYTECODE=0

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=menu-service
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV OTEL_TRACES_EXPORTER=otlp
ENV OTEL_METRICS_EXPORTER=none
ENV OTEL_LOGS_EXPORTER=none

# Expose gRPC port
EXPOSE 50054

# Health check using grpc-health-probe (proper gRPC health checking)
HEALTHCHECK --interval=2s --timeout=2s --start-period=1s --retries=3 \
    CMD ["/bin/grpc_health_probe", "-addr=:50054"]

# Chainguard's default user is already nonroot (65532); make it explicit
USER nonroot

# Run the server (chainguard python image has python as ENTRYPOINT)
CMD ["services/menu/server.py"]
