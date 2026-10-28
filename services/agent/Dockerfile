# Agent Service Dockerfile
# Multi-stage build for a small, static binary.
#
# Build context is the REPO ROOT (compose uses `context: .`), so all COPY
# paths are relative to the repository root (services/agent/...).

# Stage 1: Build the Go binary
FROM --platform=$BUILDPLATFORM golang:1.27-alpine AS builder

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
FROM alpine:3.24@sha256:be2656061b536c1883c4034790e21d0a09c5aaaf51968b89368a3c0371ea2c9a

# wget powers the compose healthcheck; ca-certificates for outbound TLS.
# Packages are deliberately unpinned: Alpine drops superseded versions from its
# index, so apk pins broke the build repeatedly (#1218, #1241, #1764). The base
# image digest above gives reproducibility; Renovate bumps the digest.
# hadolint ignore=DL3018
RUN apk add --no-cache wget ca-certificates

COPY --from=builder /agent /agent

# 4317: OTLP gRPC receiver, 13134: HTTP health endpoint
EXPOSE 4317 13134

ENTRYPOINT ["/agent"]
