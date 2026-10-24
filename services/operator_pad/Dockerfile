# Multi-stage build for the JoustMania operator pad service (UC-015, #2050 PR7)
#
# Same layout as the menu image (generic python:3.14-slim builder, Chainguard
# runtime, nonroot). One difference: the `device` extra pulls python-evdev, which
# ships as a source distribution only on arm64 (the Pi), so the builder stage
# needs a C compiler and the kernel's input headers (linux/input.h).

# Build stage - generic slim base + uv + the evdev toolchain
FROM python:3.14-slim AS builder

# gcc + libc6-dev: compile the evdev C extension; linux-libc-dev: linux/input.h
# and linux/input-event-codes.h, which evdev's build reads its key codes from.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    linux-libc-dev \
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
COPY services/operator_pad/pyproject.toml /app/services/operator_pad/

# Install workspace packages and service dependencies, with the evdev binding.
# --no-binary evdev: amd64 compiles it from source too, so the amd64 PR build
# proves the same toolchain the arm64 (Pi) build needs.
WORKDIR /app
RUN uv pip install --system -e proto/ -e lib/ --no-binary evdev -e "services/operator_pad/[device]" \
    && python -c "import evdev"

# Runtime stage - Chainguard python (glibc, Python 3.14), same digest as the
# other Python services. The compiled evdev extension needs only glibc.
FROM cgr.dev/chainguard/python:latest@sha256:678e879909418cd070927d0ba1ed018be98d43929db2457c37b9b9764703678c

# Dependency fingerprint baked at build time (#925); "dev" when built outside CI.
ARG DEPS_FINGERPRINT=dev
LABEL org.joustmania.deps-fingerprint="${DEPS_FINGERPRINT}"

WORKDIR /app

# Copy installed packages from builder onto a Chainguard sys.path entry
# (chainguard python: sys.path includes /usr/lib/python3.14)
COPY --from=builder /usr/local/lib/python3.14/site-packages/ /usr/lib/python3.14/

# Copy application code
COPY --from=builder /app/pyproject.toml /app/
COPY proto/ /app/proto/
COPY lib/ /app/lib/
COPY services/operator_pad/ /app/services/operator_pad/

# Set Python path for application code
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=0

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=operator-pad
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV OTEL_TRACES_EXPORTER=otlp
ENV OTEL_METRICS_EXPORTER=none
ENV OTEL_LOGS_EXPORTER=none

# No port and no HEALTHCHECK: the pad serves nothing (a gRPC client of the
# menu, the game coordinator and the audio service, services.md §2). A missing
# pad is a normal state (UC-015 A4), not an unhealthy container.

# Chainguard's default user is nonroot (65532). /dev/input/event* is
# root:input 0660 on the host: compose adds the host's input GID (group_add,
# INPUT_GID), so the container needs neither root nor privileged.
USER nonroot

# Run the server (chainguard python image has python as ENTRYPOINT)
CMD ["services/operator_pad/server.py"]
