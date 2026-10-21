#!/usr/bin/env bash
# Generate the agent-local Go stubs committed under services/agent/gen (#778).
#
#   scripts/ci/gen-agent-protos.sh        # what `make protos-agent` runs
#   scripts/ci/gen-agent-protos.sh <dir>  # generate into <dir> instead (the drift
#                                         # check; leaves services/agent/gen alone)
#
# The agent needs grpc-go clients to drive shadow games. Unlike the
# connect-proxy gen (build-time, never committed), these stubs are committed
# INTO the agent module so `go test` and the agent Dockerfile build with no buf
# step. scripts/ci/validate-agent-protos.sh (#1801) runs this script in CI and
# fails on any difference to the committed files.
#
# Toolchain (all pinned, so a fresh run reproduces the committed bytes):
#   - buf: the bufbuild/buf:1.47.2 image (no local buf needed). Do not move it
#     to connect-proxy's buf version here: buf compiles the descriptors embedded
#     in the .pb.go files, so the bytes could change.
#   - plugins: remote BSR plugins pinned by version AND revision in
#     proto/buf.gen.agent.yaml (a new revision of the same version can change
#     the output).
# buf runs as the host user, so the output is host-owned and a plain copy
# works (#1628). Only the network part (image pull + buf generate, which fetches
# the remote plugins) is retried: 3 attempts, AGENT_PROTOS_RETRY_DELAYS apart.
#
# Exit: 0 generated, 2 generation failed (network, buf.build or Docker Hub).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

BUF_IMAGE="bufbuild/buf:1.47.2"
GEN_DIR="${1:-services/agent/gen}"
read -r -a DELAYS <<<"${AGENT_PROTOS_RETRY_DELAYS:-5 15}"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

generate() {
    rm -rf "${tmp:?}/out" && mkdir -p "$tmp/out"
    docker image inspect "$BUF_IMAGE" >/dev/null 2>&1 || docker pull -q "$BUF_IMAGE" >/dev/null || return 1
    docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$PROJECT_ROOT/proto:/workspace/proto" \
        -v "$tmp/out:/out" \
        -w /workspace/proto \
        "$BUF_IMAGE" generate --template buf.gen.agent.yaml \
        --path controller_manager_mock.proto --path game_coordinator.proto
}

echo "Generating agent-local Go stubs (committed under $GEN_DIR)..."
attempt=1
until generate; do
    if (( attempt > ${#DELAYS[@]} )); then
        echo "❌ Agent Go stub generation failed after $attempt attempts (see the error above: buf.build or Docker Hub unreachable, or a bad proto/buf.gen.agent.yaml)" >&2
        exit 2
    fi
    delay="${DELAYS[attempt - 1]}"
    echo "buf generate failed (attempt $attempt); retrying in ${delay}s..." >&2
    sleep "$delay"
    attempt=$((attempt + 1))
done

# Managed mode lays the files out by go_package (github.com/joustmania/agent/gen/<pkg>).
src="$tmp/out/github.com/joustmania/agent/gen"
if [[ ! -d "$src" ]]; then
    echo "❌ buf generate produced no $src (check go_package overrides in proto/buf.gen.agent.yaml)" >&2
    exit 2
fi
rm -rf "$GEN_DIR" && mkdir -p "$GEN_DIR"
cp -R "$src/." "$GEN_DIR/"
echo "✓ Agent Go stubs generated"
