# Connect-Go Proxy Dockerfile
# Multi-stage build for efficient image

# Stage 1: Generate proto files
FROM bufbuild/buf:1.75.0 AS proto-gen

WORKDIR /workspace
COPY proto/ ./proto/

# Create output directory structure
RUN mkdir -p gen

# Generate Go code from protos (using Docker-specific config)
WORKDIR /workspace/proto
RUN buf generate --template buf.gen.go.docker.yaml

# Move generated files to expected location and create go.mod files
WORKDIR /workspace
RUN mv gen/github.com/joustmania/connect-proxy/gen/* gen/ && \
    rm -rf gen/github.com && \
    for dir in gen/*/; do \
      pkg=$(basename "$dir"); \
      echo "module github.com/joustmania/connect-proxy/gen/$pkg" > "$dir/go.mod"; \
      echo "" >> "$dir/go.mod"; \
      echo "go 1.25" >> "$dir/go.mod"; \
      echo "" >> "$dir/go.mod"; \
      echo "require (" >> "$dir/go.mod"; \
      echo "  connectrpc.com/connect v1.17.0" >> "$dir/go.mod"; \
      echo "  google.golang.org/protobuf v1.35.1" >> "$dir/go.mod"; \
      echo "  google.golang.org/grpc v1.67.1" >> "$dir/go.mod"; \
      echo ")" >> "$dir/go.mod"; \
    done

# Stage 2: Build the Go binary
FROM --platform=$BUILDPLATFORM golang:1.27-alpine AS builder

# Install git for go mod (needed for some dependencies)
RUN apk add --no-cache git

WORKDIR /app

# Copy go.mod and download dependencies
COPY services/connect-proxy/go.mod services/connect-proxy/go.sum* ./
RUN go mod download || true

# Copy generated proto files (with go.mod files)
COPY --from=proto-gen /workspace/gen ./gen

# Copy source code
COPY services/connect-proxy/*.go ./

# Download dependencies (after proto files are in place)
RUN go mod tidy && go mod download

# Run the unit tests (canary handlers, #1364). The generated proto packages
# only exist inside this build, so the image build is where they can run.
RUN go vet ./... && go test ./...

# Build arguments for cross-compilation. Declared after the test step so the
# test layer does not depend on TARGETARCH and runs once in multi-arch builds.
ARG TARGETARCH

# Build the binary (cross-compile for target architecture)
RUN CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" go build -ldflags="-s -w" -o /connect-proxy .

# Stage 3: Runtime image (alpine for wget healthcheck support)
FROM alpine:3.24

# Install wget for health checks
RUN apk add --no-cache wget ca-certificates

COPY --from=builder /connect-proxy /connect-proxy

EXPOSE 8080

ENTRYPOINT ["/connect-proxy"]
