# Multi-stage build for JoustMania Rust HID Service
#
# Serves PairingService gRPC on port 50058.
# Requires privileged access for HID device I/O.

# =============================================================================
# Stage 1: Build Rust binary
# =============================================================================
FROM rust:1.84-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    protobuf-compiler \
    libhidapi-dev \
    libudev-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy proto file (needed at build time by tonic-build)
COPY proto/psmove_hid.proto proto/

# Copy Rust project files
COPY services/rust-hid/Cargo.toml services/rust-hid/Cargo.lock* services/rust-hid/
COPY services/rust-hid/build.rs services/rust-hid/
COPY services/rust-hid/src/ services/rust-hid/src/

WORKDIR /app/services/rust-hid
RUN cargo build --release

# =============================================================================
# Stage 2: Runtime image
# =============================================================================
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libhidapi-hidraw0 \
    libudev1 \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy grpc-health-probe for Docker health checks
COPY --from=ghcr.io/grpc-ecosystem/grpc-health-probe:v0.4.38 /ko-app/grpc-health-probe /bin/grpc_health_probe

# Copy Rust binary
COPY --from=builder /app/services/rust-hid/target/release/rust-hid /usr/local/bin/rust-hid

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=rust-hid
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
ENV RUST_LOG=info

EXPOSE 50058

HEALTHCHECK --interval=2s --timeout=2s --start-period=5s --retries=3 \
    CMD ["/bin/grpc_health_probe", "-addr=:50058"]

CMD ["rust-hid"]
