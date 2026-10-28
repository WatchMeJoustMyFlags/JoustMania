# Flag service (epic #1407, UC-053): in-memory runtime flags, served to flagd as
# a flagd.sync.v1 gRPC sync source. Multi-stage build of a static binary.
#
# Build context is the REPO ROOT (compose uses `context: .`), so COPY paths are
# relative to the repository root.

FROM --platform=$BUILDPLATFORM golang:1.27-alpine AS builder

ARG TARGETARCH

WORKDIR /app

# go.mod/go.sum first so `go mod download` is cached across source edits. The
# flagd sync stubs come from the module proxy (buf.build/gen/go/open-feature/flagd).
COPY services/flag-service/go.mod services/flag-service/go.sum ./
RUN go mod download

COPY services/flag-service/ ./

RUN CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" \
    go build -ldflags="-s -w" -o /flag-service .

# The state volume's mount point, owned by the nonroot user: docker copies the
# ownership into a fresh named volume on first mount, so the service can write
# its admin state file (FLAG_SERVICE_STATE_FILE).
RUN mkdir -p /out/var/lib/flag-service

# Runtime: distroless static, nonroot (uid 65532). No shell: the compose health
# check runs `/flag-service healthcheck`.
FROM gcr.io/distroless/static-debian13:nonroot

COPY --from=builder /flag-service /flag-service
COPY --from=builder /app/registry.yaml /etc/flag-service/registry.yaml
COPY --from=builder --chown=65532:65532 /out/var/lib/flag-service /var/lib/flag-service

USER 65532:65532

# 50064: flagd.sync.v1 gRPC (flagd only). 8070: HTTP/JSON API and /healthz.
EXPOSE 50064 8070

ENTRYPOINT ["/flag-service"]
