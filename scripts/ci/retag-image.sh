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
# Env:   COPY_SLEEP (default 5), SOURCE_TAG (default main), VERIFY_ATTEMPTS (default 6), VERIFY_SLEEP (default 5)
#        DOCKER (default "docker"; tests substitute a fake)
# Exit:  0 = tag present and digest-identical; 1 = copy or verification failed;
#        2 = :main does not exist (first run / fork; caller decides).
set -euo pipefail

# shellcheck source=scripts/ci/lib-image-config.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib-image-config.sh"

image="${1:?usage: $0 <image>}"
: "${IMAGE_PREFIX:?IMAGE_PREFIX must be set}"
sha="${TARGET_SHA:-${GITHUB_SHA:-}}"
[[ -n "$sha" ]] || { echo "::error::retag-image: TARGET_SHA/GITHUB_SHA not set" >&2; exit 1; }
DOCKER="${DOCKER:-docker}"
source_ref="${IMAGE_PREFIX}/${image}:${SOURCE_TAG:-main}"
target_ref="${IMAGE_PREFIX}/${image}:${sha}"

attempts="${VERIFY_ATTEMPTS:-6}"
errf="$(mktemp)"
trap 'rm -f "$errf"' EXIT

# digest_of <ref>: print the manifest digest; stderr is kept in $errf so a
# real not-found can be told from an auth/network/registry error.
digest_of() { "$DOCKER" buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}' 2>"$errf"; }
is_not_found() { is_registry_not_found "$errf"; }

# Source lookup: retried like the verify loop; exit 2 only for a persistent
# not-found, any other persistent error is a failure (exit 1).
src_digest=""
for ((i = 1; i <= attempts; i++)); do
  if src_digest="$(digest_of "$source_ref")" && [[ -n "$src_digest" ]]; then break; fi
  src_digest=""
  ((i == attempts)) || sleep "${VERIFY_SLEEP:-5}"
done
if [[ -z "$src_digest" ]]; then
  if is_not_found; then
    echo "::warning::retag-image: ${source_ref} not found"
    exit 2
  fi
  echo "::error::retag-image: cannot read ${source_ref} after ${attempts} attempts: $(head -c 300 "$errf")"
  exit 1
fi

# Copy: 3 attempts, backoff 5 s / 10 s (COPY_SLEEP scales it for tests).
copied=0
for n in 1 2 3; do
  if "$DOCKER" buildx imagetools create --tag "$target_ref" "${IMAGE_PREFIX}/${image}@${src_digest}"; then copied=1; break; fi
  ((n == 3)) || sleep $((n * ${COPY_SLEEP:-5}))
done
[[ "$copied" == 1 ]] ||
  { echo "::error::retag-image: copy ${source_ref} (${src_digest}) -> ${target_ref} failed after 3 attempts"; exit 1; }

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
