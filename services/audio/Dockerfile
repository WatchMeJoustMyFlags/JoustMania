# Multi-stage build for JoustMania Audio Service
#
# Phase 80: Migrated to distroless with miniaudio (replaces pygame)
# - miniaudio for sound effects
# - resampy for tempo control (replaces scipy)
# - pyalsaaudio for ALSA music output
#
# Issue #79: Fixed libmvec.so.1 missing on ARM64
# - libmvec is x86-only in glibc (provides SIMD math functions)
# - QEMU cross-compilation incorrectly links to it on ARM64
# - Fix: disable GCC auto-vectorization for ARM64 builds
#
# #914: audio is the ONLY Python service needing a build toolchain (pyalsaaudio
# is sdist-only; miniaudio has no aarch64 wheel + is deliberately --no-binary).
# It apt-installs build-essential + libasound2-dev in its OWN build stage instead
# of relying on a shared builder image.

# =============================================================================
# Stage 0: Extract shared libraries and config for distroless
# =============================================================================
FROM debian:bookworm-slim AS system-libs
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends \
    libasound2-data \
    libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /extra-libs \
    && if [ "$TARGETARCH" = "arm64" ]; then \
         LIB_DIR=/usr/lib/aarch64-linux-gnu; \
       else \
         LIB_DIR=/usr/lib/x86_64-linux-gnu; \
       fi \
    && cp "$LIB_DIR"/libasound.so* /extra-libs/

# =============================================================================
# Stage 2: Build Python dependencies
# =============================================================================
# Toolchain installed here (not via a shared builder): build-essential for the
# C/C++ compiler, libasound2-dev for the ALSA headers pyalsaaudio compiles against.
FROM python:3.14-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libasound2-dev \
    && rm -rf /var/lib/apt/lists/*

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
COPY services/audio/pyproject.toml /app/services/audio/

# Install workspace packages and service dependencies (miniaudio + soxr instead of pygame + scipy)
# numpy and soxr install from prebuilt wheels (#911 dropped resampy/numba);
# only miniaudio and pyalsaaudio need compilation here.
# ARM64: disable auto-vectorization to prevent libmvec.so dependency (x86-only in glibc)
WORKDIR /app
ARG TARGETARCH
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      export CFLAGS="-fno-tree-loop-vectorize -fno-tree-slp-vectorize"; \
    fi && \
    uv pip install --system -e proto/ -e lib/ --no-binary miniaudio -e services/audio/

# =============================================================================
# Stage 3: Runtime image - Chainguard distroless-style for minimal size
# =============================================================================
# Python 3.14: gcr.io/distroless/python3-debian12 ships Python 3.11, which is
# ABI-incompatible with packages built against the 3.14 builder. Chainguard's
# python image tracks 3.14 (Wolfi/glibc). Digest-pinned because the free tier
# only publishes the moving `latest` tag (no version-specific tags).
FROM cgr.dev/chainguard/python:latest@sha256:678e879909418cd070927d0ba1ed018be98d43929db2457c37b9b9764703678c

# Dependency fingerprint baked at build time so the integration fast path can
# detect a stale :latest before mounting current source into it (#925).
# Computed by scripts/ci/deps-fingerprint.sh; read back by
# scripts/ci/check-image-freshness.sh. "dev" when built outside CI.
ARG DEPS_FINGERPRINT=dev
LABEL org.joustmania.deps-fingerprint="${DEPS_FINGERPRINT}"

WORKDIR /app

# Copy grpc-health-probe binary
# Copy grpc-health-probe from official multi-arch image
COPY --from=ghcr.io/grpc-ecosystem/grpc-health-probe:v0.4.52 /ko-app/grpc-health-probe /bin/grpc_health_probe

# Copy system shared libraries (ALSA for miniaudio/pyalsaaudio)
COPY --from=system-libs /extra-libs/ /usr/lib/

# Copy ALSA configuration files (required for PCM device resolution)
COPY --from=system-libs /usr/share/alsa/ /usr/share/alsa/

# Copy simple ALSA config that uses hw:0,0 directly (bypasses dmix)
# Can be overridden by mounting: -v /path/to/asound.conf:/etc/asound.conf:ro
# --chown to the Chainguard nonroot uid (65532): unlike the old distroless image
# (which ran as root), this image runs as nonroot, so the file must be writable
# for alsa_config._write_asound_conf to rewrite it with the detected card number.
COPY --chown=65532:65532 services/audio/asound.conf /etc/asound.conf

# Note: the Chainguard runtime image already provides root/audio/nonroot groups
# in /etc/group, so no group file is copied here (overwriting it would drop the
# nonroot:65532 group the image runs as).

# Copy installed packages from builder onto a Chainguard sys.path entry
# (chainguard python: sys.path includes /usr/lib/python3.14)
COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/lib/python3.14/

# Copy application code (assets mounted as volume in docker-compose)
COPY --from=builder /app/pyproject.toml /app/
COPY proto/ /app/proto/
COPY lib/ /app/lib/
COPY services/audio/*.py /app/services/audio/
COPY services/audio/tests/ /app/services/audio/tests/

# Set Python path for application code
ENV PYTHONPATH=/app
# Ensure Python uses bytecode cache for faster imports (Phase 47)
ENV PYTHONDONTWRITEBYTECODE=0

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=audio-service
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV OTEL_TRACES_EXPORTER=otlp
ENV OTEL_METRICS_EXPORTER=none
ENV OTEL_LOGS_EXPORTER=none

# Expose gRPC port
EXPOSE 50056

# Health check using grpc-health-probe
HEALTHCHECK --interval=2s --timeout=2s --start-period=1s --retries=3 \
    CMD ["/bin/grpc_health_probe", "-addr=:50056"]

# Chainguard's default user is already nonroot (65532); make it explicit
USER nonroot

# Run the server (chainguard python image has python as ENTRYPOINT)
CMD ["services/audio/server.py"]
