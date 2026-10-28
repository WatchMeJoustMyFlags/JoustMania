# Custom OTEL Collector: contrib components.
#
# Two-stage build:
#   1. OCB (OpenTelemetry Collector Builder) compiles a custom binary
#   2. Copy into a minimal Debian runtime image
#
# The stock contrib image is distroless (no shell), which prevents
# healthcheck probes.  This image uses debian-slim so grpc_health_probe
# or wget-based healthchecks work.

# ── Stage 1: Build with OCB ──────────────────────────────────────────
FROM golang:1.27-bookworm AS builder

ARG OCB_VERSION=0.164.0
ARG TARGETOS=linux
ARG TARGETARCH

# Go module servers blip (sum.golang.org 500, proxy.golang.org INTERNAL_ERROR,
# #1780). Checksum verification stays ON (GOSUMDB default); only fetching is
# made resilient: proxy first, direct VCS fallback, retries with backoff.
ENV GOPROXY=https://proxy.golang.org,direct \
    GOSUMDB=sum.golang.org

# Install OCB
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    for attempt in 1 2 3; do \
      go install go.opentelemetry.io/collector/cmd/builder@v${OCB_VERSION} && exit 0; \
      echo "go install failed (attempt ${attempt}/3)" >&2; \
      [ "${attempt}" -lt 3 ] && sleep $((attempt * 10)); \
    done; exit 1

WORKDIR /build
COPY builder-config.yaml .

# Build the custom collector binary
# Retried up to 3 times with backoff; the module cache mount keeps modules
# already downloaded by a failed attempt.
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    for attempt in 1 2 3; do \
      CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
        builder --config=builder-config.yaml && exit 0; \
      echo "collector build failed (attempt ${attempt}/3)" >&2; \
      [ "${attempt}" -lt 3 ] && sleep $((attempt * 15)); \
    done; exit 1

# ── Stage 2: Minimal runtime ─────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates wget && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/_output/otelcol-joustmania /otelcol

EXPOSE 4317 4318 8888 13133

ENTRYPOINT ["/otelcol"]
CMD ["--config=/etc/otel-collector-config.yaml"]
