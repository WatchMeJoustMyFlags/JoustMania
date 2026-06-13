#!/usr/bin/env bash
# Fast-path freshness guard (#925).
#
# Before the integration fast path mounts current source into pulled :latest
# images (USE_PREBUILT_IMAGES=true USE_DEV_MOUNTS=true), verify each pulled image
# was built from a deps fingerprint that matches the current checkout. If a
# deps-changing commit's publish was skipped, :latest is stale and mounting
# current source produces a confusing all-tests-error cascade. This guard turns
# that into a one-line diagnosis and a non-zero exit BEFORE any test runs.
#
# Mechanism: each service image carries an OCI label
#   org.joustmania.deps-fingerprint
# baked at build time (see services/*/Dockerfile + ci.yml docker-build). We
# recompute the expected fingerprint from the checkout and compare.
#
# Usage:
#   IMAGE_PREFIX=ghcr.io/owner/repo IMAGE_TAG=latest scripts/ci/check-image-freshness.sh
#
# Env:
#   IMAGE_PREFIX  (required)  e.g. ghcr.io/watchmejoustmyflags/joustmania
#   IMAGE_TAG     (default: latest)  tag the fast path will mount into
#
# Exit codes: 0 = all fresh; 1 = at least one image stale/unverifiable.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

: "${IMAGE_PREFIX:?IMAGE_PREFIX must be set (e.g. ghcr.io/owner/repo)}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
LABEL_KEY="org.joustmania.deps-fingerprint"

# The five Python service images the fast path mounts source into. These are the
# only images that bake source/deps; infra images (flagd, grafana, ...) are
# pinned by digest/version and never source-mounted, so they cannot go stale.
IMAGES=(
  controller-manager-service
  game-coordinator-service
  menu-service
  audio-service
  pairing-daemon-service
)

expected="$(bash "$SCRIPT_DIR/deps-fingerprint.sh")"
echo "Expected deps fingerprint (from checkout): $expected"

read_label() {
  # Pull (single-platform, runner arch) and read the label with `docker image
  # inspect`. The fast path's `docker compose pull` would pull these images
  # anyway, so this just warms the cache. We pull rather than use
  # `buildx imagetools inspect --format`, because the published images are
  # multi-platform manifest LISTS (index): on an index, the imagetools template
  # `.Image.Config.Labels` is a per-digest map and does not resolve to a single
  # config, so the label cannot be read reliably that way. `docker image
  # inspect` after pull resolves the concrete per-arch config every time.
  local ref="$1"
  docker pull --quiet "$ref" >/dev/null 2>&1 || return 0
  docker image inspect "$ref" \
    --format "{{ index .Config.Labels \"$LABEL_KEY\" }}" 2>/dev/null || true
}

stale=0
for img in "${IMAGES[@]}"; do
  ref="${IMAGE_PREFIX}/${img}:${IMAGE_TAG}"
  actual="$(read_label "$ref")"

  if [ -z "$actual" ] || [ "$actual" = "<no value>" ]; then
    echo "::error::${img}:${IMAGE_TAG} carries no ${LABEL_KEY} label" \
      "(predates the #925 freshness guard or was never published) —" \
      "image provenance unknown, fast-path unsafe; needs full build."
    stale=1
    continue
  fi

  if [ "$actual" != "$expected" ]; then
    echo "::error::pulled ${img}:${IMAGE_TAG} predates current deps fingerprint" \
      "${actual}≠${expected} — image stale, fast-path unsafe; needs full build."
    stale=1
    continue
  fi

  echo "OK ${img}:${IMAGE_TAG} fingerprint $actual matches checkout"
done

if [ "$stale" -ne 0 ]; then
  echo "::error::Fast-path freshness guard FAILED. The pulled :${IMAGE_TAG}" \
    "image(s) do not match the current dependency fingerprint, so mounting" \
    "current source would silently break the suite (the #925 stale-image" \
    "cascade). Re-run this commit after a full image build/publish, or push a" \
    "deps-touching change so CI takes the slow (full-build) path."
  exit 1
fi

echo "All service images fresh for fast path (fingerprint $expected)."
