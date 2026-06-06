# Agent Service Dockerfile
# Multi-stage build for a small, static binary.
#
# Build context is the REPO ROOT (compose uses `context: .`), so all COPY
# paths are relative to the repository root (services/agent/...).

# Stage 1: Build the Go binary
FROM --platform=$BUILDPLATFORM golang:1.25-alpine AS builder

# Build argument for cross-compilation (repo deploys to ARM64 Raspberry Pi 5)
ARG TARGETARCH

# No git needed: all dependencies resolve via the Go module proxy
WORKDIR /app

# Copy go.mod/go.sum first so `go mod download` is cached across source edits
COPY services/agent/go.mod services/agent/go.sum* ./
RUN go mod download

# Copy source (root package + subpackages)
COPY services/agent/ ./

# Build a static binary, cross-compiled for the target architecture
RUN CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" \
    go build -ldflags="-s -w" -o /agent .

# Stage 2: Runtime image (alpine for wget healthcheck support)
FROM alpine:3.23

# wget powers the compose healthcheck; ca-certificates for outbound TLS
RUN apk add --no-cache wget=1.25.0-r2 ca-certificates=20260413-r0

COPY --from=builder /agent /agent

# 4317: OTLP gRPC receiver, 13134: HTTP health endpoint
EXPOSE 4317 13134

ENTRYPOINT ["/agent"]
