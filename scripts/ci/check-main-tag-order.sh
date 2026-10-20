#!/usr/bin/env bash
# Main-tag ordering guard (#1793).
#
# Main runs are not serialised (concurrency groups by SHA), so a slow older run
# (e.g. a manual full rebuild) can finish AFTER a newer one and move IMAGE:main /
# :latest back to stale content. Before the publish job moves those tags it asks
# this script whether the image it is about to tag is at least as new as what
# :main holds. The :<sha> tag is always pushed; only the moving tags are skipped.
#
# Usage: IMAGE_PREFIX=ghcr.io/owner/repo scripts/ci/check-main-tag-order.sh <image> [<sha>]
#   <sha> defaults to $GITHUB_SHA.
#
# Env:
#   IMAGE_PREFIX      registry prefix (required unless SOURCE_REVISION is set)
#   SOURCE_TAG        tag to compare against (default: main)
#   SOURCE_REVISION   skip the registry: use this revision label value
#
# Needs git history (actions/checkout fetch-depth: 0). The label is the
# org.opencontainers.image.revision set by the build jobs (multi-arch: read from
# the amd64 config; both legs are built from the same SHA).
#
# Exit codes (never fails CI on its own; the caller decides):
#   0 = move the tags: :main missing/unlabelled, or <sha> equals / descends from it
#   3 = skip the tags: :main was built from a commit that is newer than, or
#       unrelated to, <sha>

set -euo pipefail

# shellcheck source=scripts/ci/lib-image-config.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib-image-config.sh"

SOURCE_TAG="${SOURCE_TAG:-main}"
REVISION_LABEL="org.opencontainers.image.revision"
image="${1:-}"
sha="${2:-${GITHUB_SHA:-}}"
[[ -n "$image" && -n "$sha" ]] || { echo "usage: $0 <image> [<sha>]" >&2; exit 1; }

ref="${IMAGE_PREFIX:-}/${image}:${SOURCE_TAG}"
if [[ -n "${SOURCE_REVISION+x}" ]]; then
  revision="$SOURCE_REVISION"
else
  : "${IMAGE_PREFIX:?IMAGE_PREFIX must be set (e.g. ghcr.io/owner/repo)}"
  if ! config_json="$(read_image_config "$ref")"; then
    echo "check-main-tag-order: ${ref} does not exist yet; moving the tags"
    exit 0
  fi
  revision="$(jq -r --arg l "$REVISION_LABEL" '.config.Labels[$l] // ""' <<<"$config_json")"
fi
[[ "$revision" == "<no value>" ]] && revision=""
if [[ -z "$revision" ]]; then
  echo "::warning::check-main-tag-order: ${ref} has no ${REVISION_LABEL} label; moving the tags"
  exit 0
fi

if [[ "$revision" == "$sha" ]]; then
  echo "check-main-tag-order: ${ref} is already ${sha:0:8} (rerun); moving the tags"
  exit 0
fi

# A newer main commit may have landed after this run's checkout: look it up.
if ! git cat-file -e "${revision}^{commit}" 2>/dev/null; then
  git fetch --quiet origin main 2>/dev/null || true
fi
if ! git cat-file -e "${revision}^{commit}" 2>/dev/null; then
  echo "::notice::check-main-tag-order: ${ref} was built from ${revision:0:8}, unknown to this checkout (newer than ${sha:0:8}); keeping :${SOURCE_TAG}, only :${sha:0:8} is pushed"
  exit 3
fi
if git merge-base --is-ancestor "$revision" "$sha"; then
  echo "check-main-tag-order: ${sha:0:8} descends from ${ref} (${revision:0:8}); moving the tags"
  exit 0
fi
echo "::notice::check-main-tag-order: ${ref} was built from ${revision:0:8}, which is newer than ${sha:0:8}; keeping :${SOURCE_TAG}/:latest, only :${sha:0:8} is pushed (#1793)"
exit 3
