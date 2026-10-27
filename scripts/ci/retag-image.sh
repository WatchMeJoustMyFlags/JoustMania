#!/usr/bin/env bash
# Retag IMAGE:main -> IMAGE:<sha> and PROVE the new tag resolves (#2120).
#
# The retag-skipped job used to run `imagetools create` with continue-on-error
# and a warning step, so a copy that failed or never landed still left the job
# green and the commit without a pinnable image (UC-060 pins an exact sha).
# This script resolves :main to a digest once, copies by digest (so a :main
# that moves mid-job cannot change what is copied), then re-reads
# IMAGE:<sha> from the registry (with retries for registry propagation) and
# requires the same digest. Anything else exits 1.
#
# Usage: IMAGE_PREFIX=ghcr.io/owner/repo TARGET_SHA=<sha> scripts/ci/retag-image.sh <image>
# Env:   SOURCE_TAG (default main), VERIFY_ATTEMPTS (default 6), VERIFY_SLEEP (default 5)
#        DOCKER (default "docker"; tests substitute a fake)
# Exit:  0 = tag present and digest-identical; 1 = copy or verification failed;
#        2 = :main does not exist (first run / fork; caller decides).
set -euo pipefail

image="${1:?usage: $0 <image>}"
: "${IMAGE_PREFIX:?IMAGE_PREFIX must be set}"
sha="${TARGET_SHA:-${GITHUB_SHA:-}}"
[[ -n "$sha" ]] || { echo "::error::retag-image: TARGET_SHA/GITHUB_SHA not set" >&2; exit 1; }
DOCKER="${DOCKER:-docker}"
source_ref="${IMAGE_PREFIX}/${image}:${SOURCE_TAG:-main}"
target_ref="${IMAGE_PREFIX}/${image}:${sha}"

digest_of() { "$DOCKER" buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}' 2>/dev/null; }

if ! src_digest="$(digest_of "$source_ref")" || [[ -z "$src_digest" ]]; then
  echo "::warning::retag-image: ${source_ref} not found"
  exit 2
fi

"$DOCKER" buildx imagetools create --tag "$target_ref" "${IMAGE_PREFIX}/${image}@${src_digest}" ||
  { echo "::error::retag-image: copy ${source_ref} (${src_digest}) -> ${target_ref} failed"; exit 1; }

attempts="${VERIFY_ATTEMPTS:-6}"
for ((i = 1; i <= attempts; i++)); do
  if got="$(digest_of "$target_ref")" && [[ "$got" == "$src_digest" ]]; then
    echo "retag-image: ${target_ref} resolves to ${got}"
    exit 0
  fi
  echo "retag-image: ${target_ref} not verified yet (got '${got:-none}', want ${src_digest}), attempt ${i}/${attempts}"
  ((i == attempts)) || sleep "${VERIFY_SLEEP:-5}"
done
echo "::error::retag-image: ${target_ref} does not resolve to ${src_digest} after ${attempts} attempts; the commit has no pinnable ${image} image"
exit 1
