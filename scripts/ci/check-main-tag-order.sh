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
#   REPO_DIR          git checkout to compare in (default: $GITHUB_WORKSPACE, else
#                     the repo this script lives in). Every git call uses it, so the
#                     caller's cwd does not matter (the publish step runs in the
#                     digests dir, outside the checkout).
#
# Needs the commit graph (actions/checkout fetch-depth: 0; a blob:none partial
# clone is enough). The label is the org.opencontainers.image.revision set by the
# build jobs (multi-arch: read from the amd64 config; both legs share the SHA).
#
# Tags are skipped only when it is PROVEN that :main is newer (<sha> is an
# ancestor of its revision). A revision unknown even after fetching origin/main,
# or unrelated to <sha>, is not on main: the tags move with a ::warning::.
#
# Exit codes:
#   0 = move the tags
#   3 = skip the tags: :main was built from a descendant of <sha>
#   2 = git fetch of origin main failed (the caller fails the step)
#   1 = usage

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

repo_dir="${REPO_DIR:-${GITHUB_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && git rev-parse --show-toplevel)}}"
g() { git -C "$repo_dir" "$@"; }

# A newer main commit may have landed after this run's checkout: look it up.
if ! g cat-file -e "${revision}^{commit}" 2>/dev/null; then
  if ! g fetch --quiet origin main; then
    echo "::error::check-main-tag-order: git fetch origin main failed in ${repo_dir}; cannot order ${ref} (${revision:0:8}) against ${sha:0:8}"
    exit 2
  fi
fi
if ! g cat-file -e "${revision}^{commit}" 2>/dev/null; then
  echo "::warning::check-main-tag-order: ${ref} was built from ${revision:0:8}, which is not on origin/main; moving the tags"
  exit 0
fi
if g merge-base --is-ancestor "$revision" "$sha"; then
  echo "check-main-tag-order: ${sha:0:8} descends from ${ref} (${revision:0:8}); moving the tags"
  exit 0
fi
if g merge-base --is-ancestor "$sha" "$revision"; then
  echo "::notice::check-main-tag-order: ${ref} was built from ${revision:0:8}, which descends from ${sha:0:8}; keeping :${SOURCE_TAG}/:latest, only :${sha:0:8} is pushed (#1793)"
  exit 3
fi
echo "::warning::check-main-tag-order: ${ref} was built from ${revision:0:8}, unrelated to ${sha:0:8}; moving the tags"
exit 0
