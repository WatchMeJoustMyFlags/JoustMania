#!/usr/bin/env bash
# Validate that the committed agent Go stubs (services/agent/gen) match a fresh
# generation from proto/*.proto (#1801).
#
#   make ci-validate-agent-protos           # CI: the "Validate agent Go stubs" step
#
# The Python stubs have scripts/ci/validate-protos.sh; nothing checked the Go
# stubs, so a hand edit (#1790) or a proto change without `make protos-agent`
# went unnoticed. This regenerates with scripts/ci/gen-agent-protos.sh (pinned
# buf image and plugin revisions) and compares with `git status --porcelain`,
# which reports modified, deleted AND untracked files (`git diff` misses the
# last two, #904).
#
# Exit: 0 in sync, 1 drift, 2 generation failed (not drift: network or tooling).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

GEN_DIR="services/agent/gen"

echo "Validating agent Go stubs ($GEN_DIR)..."
if ! bash "$SCRIPT_DIR/gen-agent-protos.sh"; then
    echo "❌ Could not generate the agent Go stubs, so drift was not checked; see the generation error above (a network error: re-run the job)." >&2
    exit 2
fi

status="$(git status --porcelain --untracked-files=all -- "$GEN_DIR")"
if [[ -n "$status" ]]; then
    echo "❌ Agent Go stubs are out of sync with proto/*.proto! Run 'make protos-agent' and commit the changes."
    echo "Divergent files (M modified, D deleted by regeneration, ?? new from regeneration):"
    echo "$status"
    echo "--- diff (tracked files) ---"
    git --no-pager diff -- "$GEN_DIR" || true
    exit 1
fi

echo "✅ Agent Go stubs validated!"
