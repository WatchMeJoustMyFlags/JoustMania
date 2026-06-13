#!/usr/bin/env bash
# Compute a stable PER-SERVICE "dependency fingerprint" for a source-mounted
# Python service image (#925).
#
# The fast path (USE_PREBUILT_IMAGES=true USE_DEV_MOUNTS=true) mounts the current
# source tree into pulled :latest images. That is only safe when the pulled image
# already contains every dependency the current source imports. If a deps-changing
# commit's image publish was skipped (e.g. a flake skipped the publish job), :latest
# goes stale and the fast path mounts incompatible source -> the whole suite errors
# with no obvious cause (the 2026-06-09 soxr cascade).
#
# Usage:
#   deps-fingerprint.sh <service>
#     where <service> is one of the source-mounted Python services, e.g.
#     menu, audio, controller_manager, game_coordinator, pairing_daemon.
#
# WHY PER-SERVICE (not one shared hash): the CI fast path rebuilds+republishes
# each service's :latest only when ITS OWN path filter fires — the shared_python
# anchor (root pyproject, proto/lib pyproject, images/builder/**) OR
# services/<svc>/**. A single shared fingerprint over every service's files would
# change when ANY service's deps change, even ones that weren't rebuilt, so the
# guard would FALSE-fail on the unchanged images and wedge the fast path on main —
# the exact failure this guard exists to prevent. A per-service fingerprint changes
# exactly when THAT image must be rebuilt.
#
# This script hashes ONLY the files that determine what gets BAKED into the named
# service image (NOT application .py source — the fast path mounts that fresh):
#
#   Shared deps inputs (baked into every service image via the builder + workspace):
#     - root pyproject.toml          (workspace deps)
#     - proto/pyproject.toml         (shared proto package deps)
#     - lib/pyproject.toml           (shared lib package deps)
#     - images/builder/**            (builder Dockerfile + requirements-common.txt;
#                                     everything the shared builder bakes)
#   This service's OWN deps inputs:
#     - services/<svc>/pyproject.toml  (service dependencies)
#     - services/<svc>/Dockerfile      (how deps are installed / base image)
#
# This mirrors the per-service rebuild condition (shared_python OR services/<svc>/**)
# restricted to deps-determining files.
#
# The same value is:
#   1. baked into the service image as the OCI label org.joustmania.deps-fingerprint
#      at build time (build-arg DEPS_FINGERPRINT, see services/*/Dockerfile), and
#   2. recomputed from the checkout by scripts/ci/check-image-freshness.sh, which
#      reads the label back from the pulled image and compares.
#
# Determinism: files are sorted and hashed by content (filename + bytes), so the
# value is independent of filesystem order and reproducible on any checkout.

set -euo pipefail

SERVICE="${1:?usage: deps-fingerprint.sh <service> (e.g. menu, audio, controller_manager)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -d "services/$SERVICE" ]; then
  echo "deps-fingerprint.sh: unknown service '$SERVICE' (no services/$SERVICE)" >&2
  exit 2
fi

# Collect the dependency-determining files for THIS service (NUL-safe), sorted
# for determinism. Shared deps inputs + this service's own pyproject/Dockerfile.
mapfile -d '' -t files < <(
  {
    printf '%s\0' pyproject.toml
    printf '%s\0' proto/pyproject.toml
    printf '%s\0' lib/pyproject.toml
    # Everything the shared builder bakes (Dockerfile + requirements-common.txt).
    find images/builder -type f -print0
    printf '%s\0' "services/$SERVICE/pyproject.toml"
    printf '%s\0' "services/$SERVICE/Dockerfile"
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
