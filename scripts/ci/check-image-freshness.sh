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
# baked at build time (see services/*/Dockerfile + ci.yml docker-build). The
# fingerprint is PER-SERVICE (scripts/ci/deps-fingerprint.sh <svc>), scoped to
# exactly the deps inputs that rebuild THAT image (shared workspace/builder deps +
# the service's own pyproject/Dockerfile). We recompute each service's expected
# fingerprint from the checkout and compare against that image's label, so a deps
# change to one service never false-stales the others.
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

# The five source-mounted Python services, as "service:image-name" pairs. The
# fast path mounts current source into these images; infra images (flagd,
# grafana, ...) are pinned by digest/version and never source-mounted, so they
# cannot go stale. The service name (left) feeds deps-fingerprint.sh; the image
# name (right) is the published repository.
SERVICES=(
  "controller_manager:controller-manager-service"
  "game_coordinator:game-coordinator-service"
  "menu:menu-service"
  "audio:audio-service"
  "pairing_daemon:pairing-daemon-service"
)

# Pull the image (single-platform, runner arch). We pull rather than use
# `buildx imagetools inspect --format`, because the published images are
# multi-platform manifest LISTS (index): on an index, the imagetools template
# `.Image.Config.Labels` is a per-digest map and does not resolve to a single
# config, so the label cannot be read reliably that way. The fast path's
# `docker compose pull` would pull these images anyway, so this just warms the
# cache. Returns non-zero on pull failure so the caller can distinguish a
# transient network/registry error from a genuinely missing label.
pull_image() {
  local ref="$1"
  docker pull --quiet "$ref" >/dev/null 2>&1
}

read_label() {
  # Read the label from an already-pulled image. Empty when the image carries
  # no such label (predates the guard / never baked it).
  local ref="$1"
  docker image inspect "$ref" \
    --format "{{ index .Config.Labels \"$LABEL_KEY\" }}" 2>/dev/null || true
}

stale=0
for entry in "${SERVICES[@]}"; do
  svc="${entry%%:*}"
  img="${entry##*:}"
  ref="${IMAGE_PREFIX}/${img}:${IMAGE_TAG}"

  # Per-service expected fingerprint, recomputed from THIS checkout.
  expected="$(bash "$SCRIPT_DIR/deps-fingerprint.sh" "$svc")"

  # Distinguish a pull failure (network/registry — different operator action:
  # retry / check auth) from a missing label (real staleness signal). A failed
  # pull must NOT be silently reported as "needs full build".
  if ! pull_image "$ref"; then
    echo "::error::failed to pull ${img}:${IMAGE_TAG} from ${IMAGE_PREFIX}" \
      "— this is a NETWORK/REGISTRY error, not image staleness. Check GHCR" \
      "auth/availability and re-run; do NOT treat as a needs-rebuild signal."
    stale=1
    continue
  fi

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
      "(${svc}) ${actual}≠${expected} — image stale, fast-path unsafe;" \
      "needs full build."
    stale=1
    continue
  fi

  echo "OK ${img}:${IMAGE_TAG} fingerprint $actual matches checkout ($svc)"
done

if [ "$stale" -ne 0 ]; then
  echo "::error::Fast-path freshness guard FAILED. The pulled :${IMAGE_TAG}" \
    "image(s) either could not be verified or do not match the current" \
    "dependency fingerprint, so mounting current source would silently break" \
    "the suite (the #925 stale-image cascade). Resolve the cause above" \
    "(network vs stale image), then re-run; for a stale image, push a" \
    "deps-touching change so CI takes the slow (full-build) path."
  exit 1
fi

echo "All service images fresh for fast path."
