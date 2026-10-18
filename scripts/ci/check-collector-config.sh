#!/usr/bin/env bash
# Fast-path collector guard (#1487).
#
# The integration fast path pulls the published otel-collector image but
# bind-mounts the CHECKED-OUT services/otel-collector/config.yaml into it. When
# the config starts using a component the pulled image does not have (#1472
# moved onto the span_metrics connector while :latest was still a June build),
# the collector exits 1 at startup and every integration test errors in setup.
# The #925 deps-fingerprint guard does not cover this: the collector has no
# source mount, only a config mount.
#
# This runs the pulled image's own `validate --config` against the checkout's
# config, so a mismatch is caught before `compose up`, whatever its cause.
#
# Usage:
#   scripts/ci/check-collector-config.sh [--no-pull] <image-ref> [config]
#     config defaults to services/otel-collector/config.yaml
#     --no-pull: validate the image already present locally and never pull. Use
#     it after building HEAD under <image-ref>, otherwise the pull replaces that
#     build with the (stale) registry image.
#
# Exit codes: 0 = the image accepts the config; 1 = it rejects it (stale image
# or a broken config; the validate output says which component); 2 = the image
# could not be pulled (or, with --no-pull, is not present locally).

set -euo pipefail

no_pull=0
if [[ "${1-}" == "--no-pull" ]]; then
  no_pull=1
  shift
fi
ref="${1:?usage: $0 [--no-pull] <image-ref> [config]}"
config="${2:-services/otel-collector/config.yaml}"
[[ -f "$config" ]] || {
  echo "::error::check-collector-config: ${config} not found" >&2
  exit 1
}
config="$(cd "$(dirname "$config")" && pwd)/$(basename "$config")"

if [[ "$no_pull" -eq 1 ]]; then
  if ! docker image inspect "$ref" >/dev/null 2>&1; then
    echo "::warning::check-collector-config: ${ref} is not present locally (--no-pull)"
    exit 2
  fi
elif ! docker pull --quiet "$ref" >/dev/null 2>&1 &&
  ! docker image inspect "$ref" >/dev/null 2>&1; then
  echo "::warning::check-collector-config: could not pull ${ref}"
  exit 2
fi

# The hostmetrics receivers stat their root_path (/hostfs/root in compose) during
# validation. An empty directory satisfies that without mounting the host.
hostfs="$(mktemp -d)"
trap 'rm -rf "$hostfs"' EXIT

if out="$(docker run --rm --network none \
  -v "${config}:/etc/otel-collector-config.yaml:ro" \
  -v "${hostfs}:/hostfs/root:ro" \
  "$ref" validate --config=/etc/otel-collector-config.yaml 2>&1)"; then
  echo "check-collector-config: ${ref} accepts ${config}"
  exit 0
fi
echo "$out"
echo "::warning::check-collector-config: ${ref} rejects the checked-out collector config (image older than the config?)"
exit 1
