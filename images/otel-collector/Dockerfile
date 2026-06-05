# Custom OTEL Collector: contrib components + dtbizevents exporter.
#
# Two-stage build:
#   1. OCB (OpenTelemetry Collector Builder) compiles a custom binary
#   2. Copy into a minimal Debian runtime image
#
# The stock contrib image is distroless (no shell), which prevents
# healthcheck probes.  This image uses debian-slim so grpc_health_probe
# or wget-based healthchecks work.

# ── Stage 1: Build with OCB ──────────────────────────────────────────
FROM golang:1.26-bookworm AS builder

ARG OCB_VERSION=0.147.0
ARG TARGETOS=linux
ARG TARGETARCH

# Install OCB
RUN go install go.opentelemetry.io/collector/cmd/builder@v${OCB_VERSION}

WORKDIR /build
COPY builder-config.yaml .
COPY dtbizeventsexporter/ ./dtbizeventsexporter/

# Build the custom collector binary
RUN CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
    builder --config=builder-config.yaml

# ── Stage 2: Minimal runtime ─────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates wget && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/_output/otelcol-joustmania /otelcol

EXPOSE 4317 4318 8888 8889 13133

ENTRYPOINT ["/otelcol"]
CMD ["--config=/etc/otel-collector-config.yaml"]
