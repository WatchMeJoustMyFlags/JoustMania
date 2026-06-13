#!/usr/bin/env bash
# Compute a stable "dependency fingerprint" for the service images (#925).
#
# The fast path (USE_PREBUILT_IMAGES=true USE_DEV_MOUNTS=true) mounts the current
# source tree into pulled :latest images. That is only safe when the pulled image
# already contains every dependency the current source imports. If a deps-changing
# commit's image publish was skipped (e.g. a flake skipped the publish job), :latest
# goes stale and the fast path mounts incompatible source -> the whole suite errors
# with no obvious cause (the 2026-06-09 soxr cascade).
#
# This script hashes the files that determine what gets baked into a service image:
#   - root workspace pyproject.toml
#   - proto/pyproject.toml, lib/pyproject.toml (shared packages every image installs)
#   - every services/*/pyproject.toml      (per-service dependencies)
#   - every services/*/Dockerfile          (how deps are installed / which base image)
#
# The same value is:
#   1. baked into each service image as the OCI label org.joustmania.deps-fingerprint
#      at build time (build-arg DEPS_FINGERPRINT, see services/*/Dockerfile), and
#   2. recomputed from the checkout by scripts/ci/check-image-freshness.sh, which
#      reads the label back from the pulled image and compares.
#
# Determinism: files are sorted and hashed by content (filename + bytes), so the
# value is independent of filesystem order and reproducible on any checkout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

# Collect the dependency-determining files (NUL-safe), sorted for determinism.
mapfile -d '' -t files < <(
  {
    printf '%s\0' pyproject.toml
    printf '%s\0' proto/pyproject.toml
    printf '%s\0' lib/pyproject.toml
    find services -maxdepth 2 \( -name pyproject.toml -o -name Dockerfile \) -print0
  } | sort -z
)

# Hash filename + content of each file so a rename also changes the fingerprint.
hash_stream() {
  local f
  for f in "${files[@]}"; do
    [ -f "$f" ] || continue
    printf '%s\0' "$f"
    cat "$f"
    printf '\0'
  done
}

# sha256sum is available on CI runners and in the builder image.
hash_stream | sha256sum | cut -c1-16
