#!/usr/bin/env bash
# Release retag for ONE image: IMAGE:<sha> -> IMAGE:<version> (release.yml tag-images, #2338).
#
# release-please runs from the CI workflow_run of an EARLIER commit, so when it creates
# the release the tag commit's own CI is often still queued and IMAGE:<sha> does not
# exist yet (0.9.0 and 0.8.0 needed a manual retag). This script therefore waits,
# bounded, for IMAGE:<sha> to appear (exponential backoff), then copies it by digest.
#
# Guards (unchanged from the inline step it replaces):
#   - an existing IMAGE:<version> is never overwritten with a different digest;
#   - only a registry "not found" counts as missing; auth, network, registry or
#     rate-limit errors fail closed at once (nothing is created);
#   - after the copy, IMAGE:<version> must resolve to the source digest.
# The release tag pointing at <sha> is checked by release.yml's resolve job.
#
# Usage: IMAGE_PREFIX=ghcr.io/owner/repo VERSION=0.9.1 SHA=<40-hex> scripts/ci/release-retag.sh <image>
# Env:   WAIT_TIMEOUT (s, default 2700 = 45 min), WAIT_SLEEP (first backoff s, default 20),
#        WAIT_MAX_SLEEP (backoff cap s, default 300), VERIFY_ATTEMPTS (default 6),
#        VERIFY_SLEEP (default 5), REPO (owner/repo, for the dispatch hint),
#        DOCKER (default "docker"; tests substitute a fake)
# Exit:  0 = IMAGE:<version> resolves to IMAGE:<sha>'s digest; 1 = anything else.
set -euo pipefail

# shellcheck source=scripts/ci/lib-image-config.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib-image-config.sh"

image="${1:?usage: $0 <image>}"
: "${IMAGE_PREFIX:?IMAGE_PREFIX must be set}"
: "${VERSION:?VERSION must be set}"
: "${SHA:?SHA must be set}"
DOCKER="${DOCKER:-docker}"
src_ref="${IMAGE_PREFIX}/${image}:${SHA}"
dst_ref="${IMAGE_PREFIX}/${image}:${VERSION}"
timeout_s="${WAIT_TIMEOUT:-2700}"
sleep_s="${WAIT_SLEEP:-20}"
max_sleep_s="${WAIT_MAX_SLEEP:-300}"
dispatch_hint="gh workflow run release.yml${REPO:+ --repo ${REPO}} -f version=${VERSION} -f sha=${SHA}"

errf="$(mktemp)"
trap 'rm -f "$errf"' EXIT

# digest_of <ref>: print the manifest digest; stderr goes to $errf so a missing tag
# can be told from an auth/network/registry error.
digest_of() { "$DOCKER" buildx imagetools inspect "$1" --format '{{.Manifest.Digest}}' 2>"$errf"; }

# 1. Wait for the source tag (the tag commit's CI may still be queued or running).
waited=0
while :; do
  if src="$(digest_of "$src_ref")" && [[ -n "$src" ]]; then break; fi
  if ! is_registry_not_found "$errf"; then
    echo "::error::release-retag: cannot inspect ${src_ref}: $(head -c 300 "$errf"); failing closed, nothing tagged"
    exit 1
  fi
  if ((waited >= timeout_s)); then
    echo "::error::release-retag: ${src_ref} still missing after ${waited}s; the CI run for ${SHA} did not publish ${image}. Once that commit's CI has passed, rerun the retag: ${dispatch_hint}"
    exit 1
  fi
  nap=$((sleep_s < timeout_s - waited ? sleep_s : timeout_s - waited))
  echo "release-retag: ${src_ref} not found yet (CI for ${SHA} still running?); waited ${waited}s of ${timeout_s}s, next check in ${nap}s"
  sleep "$nap"
  waited=$((waited + nap))
  sleep_s=$((sleep_s * 2 > max_sleep_s ? max_sleep_s : sleep_s * 2))
done
echo "release-retag: ${src_ref} is ${src}"

# 2. Never overwrite an existing version tag with a different digest.
if dst="$(digest_of "$dst_ref")" && [[ -n "$dst" ]]; then
  if [[ "$dst" == "$src" ]]; then
    echo "release-retag: ${dst_ref} already is ${src}; nothing to do"
    exit 0
  fi
  echo "::error::release-retag: ${dst_ref} already exists as ${dst}, source ${src_ref} is ${src}; not overwriting"
  exit 1
fi
if ! is_registry_not_found "$errf"; then
  echo "::error::release-retag: cannot inspect ${dst_ref}: $(head -c 300 "$errf"); failing closed, nothing tagged"
  exit 1
fi

# 3. Copy by digest (a source tag that moves mid-job cannot change what is copied), then verify.
echo "release-retag: creating ${dst_ref} -> ${src} (from ${src_ref})"
"$DOCKER" buildx imagetools create --tag "$dst_ref" "${IMAGE_PREFIX}/${image}@${src}"
attempts="${VERIFY_ATTEMPTS:-6}"
for ((i = 1; i <= attempts; i++)); do
  if got="$(digest_of "$dst_ref")" && [[ "$got" == "$src" ]]; then
    echo "release-retag: ${dst_ref} resolves to ${got}"
    exit 0
  fi
  ((i == attempts)) || sleep "${VERIFY_SLEEP:-5}"
done
echo "::error::release-retag: ${dst_ref} does not resolve to ${src} after ${attempts} attempts (got '${got:-none}'). Rerun: ${dispatch_hint}"
exit 1
