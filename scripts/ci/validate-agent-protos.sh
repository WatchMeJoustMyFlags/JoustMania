#!/usr/bin/env bash
# Validate that the committed agent Go stubs (services/agent/gen) match a fresh
# generation from proto/*.proto (#1801).
#
#   make ci-validate-agent-protos           # CI: the "Validate agent Go stubs" step
#
# The Python stubs have scripts/ci/validate-protos.sh; nothing checked the Go
# stubs, so a hand edit (#1790) or a proto change without `make protos-agent`
# went unnoticed. This generates with scripts/ci/gen-agent-protos.sh (pinned
# buf image and plugin revisions) into a temp dir and `diff -r`s it against the
# WORKING TREE, so modified, deleted and extra files all count (#904), a correct
# but uncommitted `make protos-agent` passes, and uncommitted edits under
# services/agent/gen are never overwritten.
#
# Exit: 0 in sync, 1 drift, 2 generation failed (not drift: network or tooling).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

GEN_DIR="services/agent/gen"
fresh="$(mktemp -d)"
trap 'rm -rf "$fresh"' EXIT

echo "Validating agent Go stubs ($GEN_DIR)..."
if ! bash "$SCRIPT_DIR/gen-agent-protos.sh" "$fresh"; then
    echo "❌ Agent Go stub generation failed (network, image, or proto/template error); see log above. Drift was not checked." >&2
    exit 2
fi

if ! diff -rq "$fresh" "$GEN_DIR" >/dev/null 2>&1; then
    echo "❌ Agent Go stubs are out of sync with proto/*.proto! Run 'make protos-agent' and commit the changes."
    echo "Divergent files (left side = fresh generation, right side = $GEN_DIR):"
    diff -rq "$fresh" "$GEN_DIR" || true
    echo "--- diff ---"
    diff -ru "$fresh" "$GEN_DIR" || true
    exit 1
fi

echo "✅ Agent Go stubs validated!"
