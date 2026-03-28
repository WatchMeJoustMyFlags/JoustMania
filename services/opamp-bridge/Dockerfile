# OpAMP Bridge — watches flagd flags and pushes config to OTel Collector.
#
# Multi-stage build: compile Go binary, then copy into minimal runtime.

# ── Stage 1: Build ──────────────────────────────────────────────────
FROM golang:1.23-bookworm AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
RUN CGO_ENABLED=0 go build -o /opamp-bridge .

# ── Stage 2: Runtime ────────────────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates wget && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opamp-bridge /opamp-bridge

EXPOSE 4320 8080

ENTRYPOINT ["/opamp-bridge"]
