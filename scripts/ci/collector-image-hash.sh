#!/usr/bin/env bash
# Content hash of the custom otel-collector image sources (#1780).
#
# The collector binary is compiled by OCB from images/otel-collector/
# (Dockerfile + builder-config.yaml). Its runtime config
# (services/otel-collector/config.yaml) is mounted, not baked in, so it does not
# belong in the hash. The same sources always give the same 16-char tag, so CI
# builds and pushes `otel-collector:<hash>` once and every e2e run pulls it.
#
# Usage: scripts/ci/collector-image-hash.sh [git-ref]   (default HEAD)
set -euo pipefail
ref="${1:-HEAD}"
cd "$(git rev-parse --show-toplevel)"
# `ls-tree -r` lists mode, blob id and path of every file: any content, rename
# or mode change moves the hash; the checkout state of the worktree does not.
git ls-tree -r "$ref" -- images/otel-collector | sha256sum | cut -c1-16
