# Bizevents Bridge Dockerfile
# Receives OTLP logs from metricsaslogs connector and forwards as
# Dynatrace business events via /api/v2/bizevents/ingest

# Stage 1: Build the Go binary
FROM --platform=$BUILDPLATFORM golang:1.22-alpine AS builder

# Build arguments for cross-compilation
ARG TARGETARCH

WORKDIR /app

# Copy go.mod (no external dependencies)
COPY services/bizevents-bridge/go.mod ./

# Copy source code
COPY services/bizevents-bridge/*.go ./

# Build the binary (cross-compile for target architecture)
RUN CGO_ENABLED=0 GOOS=linux GOARCH=${TARGETARCH} go build -ldflags="-s -w" -o /bizevents-bridge .

# Stage 2: Runtime image
FROM alpine:3.19

# Install wget for health checks
RUN apk add --no-cache wget ca-certificates

COPY --from=builder /bizevents-bridge /bizevents-bridge

EXPOSE 4319

# Health check
HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD wget --spider -q http://localhost:4319/health || exit 1

ENTRYPOINT ["/bizevents-bridge"]
