# Mobile Gateway Service — Go WebSocket-to-gRPC bridge for phone controllers
#
# Multi-stage build: generate proto files, build Go binary, minimal runtime.

# Stage 1: Generate Go proto files from .proto definitions
FROM bufbuild/buf:1.47.2 AS proto-gen

WORKDIR /workspace
COPY proto/ ./proto/

RUN mkdir -p gen

WORKDIR /workspace/proto
RUN buf generate --template buf.gen.mobile.yaml

# Create go.mod for the generated package
WORKDIR /workspace
RUN pkg_dir="gen/github.com/WatchMeJoustMyFlags/JoustMania/services/mobile_gateway/proto/mobile_controller" && \
    echo 'module github.com/WatchMeJoustMyFlags/JoustMania/services/mobile_gateway/proto/mobile_controller' > "$pkg_dir/go.mod" && \
    echo '' >> "$pkg_dir/go.mod" && \
    echo 'go 1.22' >> "$pkg_dir/go.mod" && \
    echo '' >> "$pkg_dir/go.mod" && \
    echo 'require (' >> "$pkg_dir/go.mod" && \
    echo '  google.golang.org/protobuf v1.35.1' >> "$pkg_dir/go.mod" && \
    echo '  google.golang.org/grpc v1.67.1' >> "$pkg_dir/go.mod" && \
    echo ')' >> "$pkg_dir/go.mod"

# Stage 2: Build the Go binary
FROM --platform=$BUILDPLATFORM golang:1.22-alpine AS builder

ARG TARGETARCH

RUN apk add --no-cache git

WORKDIR /app

# Copy go.mod and download dependencies
COPY services/mobile_gateway/go.mod services/mobile_gateway/go.sum* ./

# Copy generated proto files (overwrite local stubs)
COPY --from=proto-gen /workspace/gen/github.com/WatchMeJoustMyFlags/JoustMania/services/mobile_gateway/proto/mobile_controller ./proto/mobile_controller/

# Download dependencies
RUN go mod tidy && go mod download

# Copy source code
COPY services/mobile_gateway/*.go ./

# Build the binary (cross-compile for target architecture)
RUN CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" go build -ldflags="-s -w" -o /mobile-gateway .

# Stage 3: Minimal runtime image
FROM alpine:3.19

RUN apk add --no-cache ca-certificates wget

COPY --from=builder /mobile-gateway /mobile-gateway
COPY services/mobile_gateway/static/ /static/

# Configure OpenTelemetry
ENV OTEL_SERVICE_NAME=mobile-gateway-service
ENV OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317

EXPOSE 8070

HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=3 \
    CMD ["wget", "-q", "--spider", "http://localhost:8070/healthz"]

CMD ["/mobile-gateway"]
