#!/usr/bin/env bash
# Retag freshness guard (#1487).
#
# On a main push, the retag-skipped job copies IMAGE:main forward as
# IMAGE:<sha> for every image the path filter did not rebuild. That is only
# correct when :main already contains the last change to the image's sources.
# If the run that should have rebuilt it was cancelled (c14cb15e, #1472), :main
# is older than its sources and the retag silently ships a stale image under a
# new sha (and the fast path then mounts new config into it).
#
# This script refuses that case: it finds the last commit that touched the
# image's source paths (the SAME `changes` filter ci.yml uses to decide whether
# to build the image) and checks that :main was built from that commit or a
# later one.
#
#   1. :main carries org.opencontainers.image.revision (set by the build jobs):
#      fresh when the last source commit is an ancestor of that revision.
#   2. No revision label (images built before the label existed): fresh when
#      the image's creation time is at or after the last source commit's
#      committer time.
#
# Usage:
#   IMAGE_PREFIX=ghcr.io/owner/repo scripts/ci/check-retag-source.sh <image>
#   scripts/ci/check-retag-source.sh --paths <image>   # print the source globs
#
# Env:
#   IMAGE_PREFIX       registry prefix (required unless both overrides are set)
#   SOURCE_TAG         tag to check (default: main)
#   SOURCE_REVISION    skip the registry: use this revision label value
#   SOURCE_CREATED     skip the registry: use this creation time (RFC 3339)
#   CI_WORKFLOW        workflow holding the filters (default: .github/workflows/ci.yml)
#
# Needs full git history (actions/checkout fetch-depth: 0) and python3 + PyYAML.
#
# Exit codes: 0 = fresh; 1 = stale or undecidable; 2 = :main tag missing.

set -euo pipefail

# shellcheck source=scripts/ci/lib-image-config.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib-image-config.sh"

CI_WORKFLOW="${CI_WORKFLOW:-.github/workflows/ci.yml}"
SOURCE_TAG="${SOURCE_TAG:-main}"
REVISION_LABEL="org.opencontainers.image.revision"

usage() {
  echo "usage: $0 [--paths] <image>" >&2
  exit 1
}

print_paths=0
if [[ "${1:-}" == "--paths" ]]; then
  print_paths=1
  shift
fi
image="${1:-}"
[[ -n "$image" ]] || usage

# Release image -> `changes` filter key. Mirrors maybe_retag in ci.yml.
case "$image" in
  controller-manager-service) filter=controller_manager ;;
  game-coordinator-service) filter=game_coordinator ;;
  menu-service) filter=menu ;;
  audio-service) filter=audio ;;
  pairing-daemon-service) filter=pairing_daemon ;;
  connect-proxy) filter=connect_proxy ;;
  dashboard) filter=dashboard ;;
  rust-hid) filter=rust ;;
  python-hid) filter=python_hid ;;
  operator-pad) filter=operator_pad ;;
  otel-collector) filter=otel_collector ;;
  flag-service) filter=flag_service ;;
  agent) filter=agent ;;
  *)
    echo "::error::check-retag-source: unknown image '${image}'" >&2
    exit 1
    ;;
esac

# The filter globs, read from the workflow so they can never drift from what
# decides "build or retag". Anchors (*shared_python) resolve to nested lists.
mapfile -t globs < <(python3 - "$CI_WORKFLOW" "$filter" <<'PY'
import sys

import yaml

workflow, key = sys.argv[1], sys.argv[2]
with open(workflow, encoding="utf-8") as fh:
    doc = yaml.safe_load(fh)
for step in doc["jobs"]["changes"]["steps"]:
    if step.get("id") == "f":
        filters = yaml.safe_load(step["with"]["filters"])
        break
else:
    sys.exit("no paths-filter step with id 'f' in " + workflow)


def flatten(items):
    for item in items:
        if isinstance(item, list):
            yield from flatten(item)
        else:
            yield item


if key not in filters:
    sys.exit(f"filter '{key}' not found in {workflow}")
for glob in flatten(filters[key]):
    print(glob)
PY
)
if [[ ${#globs[@]} -eq 0 ]]; then
  echo "::error::check-retag-source: no source paths for ${image} (filter ${filter})" >&2
  exit 1
fi

if [[ "$print_paths" -eq 1 ]]; then
  printf '%s\n' "${globs[@]}"
  exit 0
fi

pathspecs=()
for g in "${globs[@]}"; do pathspecs+=(":(glob)${g}"); done

last_line="$(git log -1 --format='%H %ct' HEAD -- "${pathspecs[@]}")"
if [[ -z "$last_line" ]]; then
  echo "check-retag-source: no commit touches ${image}'s sources; nothing to compare"
  exit 0
fi
last_sha="${last_line%% *}"
last_time="${last_line##* }"

ref="${IMAGE_PREFIX:-}/${image}:${SOURCE_TAG}"
revision="${SOURCE_REVISION-}"
created="${SOURCE_CREATED-}"
if [[ -z "${SOURCE_REVISION+x}" || -z "${SOURCE_CREATED+x}" ]]; then
  : "${IMAGE_PREFIX:?IMAGE_PREFIX must be set (e.g. ghcr.io/owner/repo)}"
  # Read the amd64 image config straight from the registry (no layers pulled).
  rc=0
  config_json="$(read_image_config "$ref")" || rc=$?
  if [[ "$rc" == 1 ]]; then
    echo "::warning::check-retag-source: ${ref} not found"
    exit 2
  elif [[ "$rc" != 0 ]]; then
    echo "::error::check-retag-source: cannot read ${ref} (registry error, not a missing tag)"
    exit 1
  fi
  [[ -n "${SOURCE_REVISION+x}" ]] ||
    revision="$(jq -r --arg l "$REVISION_LABEL" '.config.Labels[$l] // ""' <<<"$config_json")"
  [[ -n "${SOURCE_CREATED+x}" ]] ||
    created="$(jq -r '.created // ""' <<<"$config_json")"
fi
[[ "$revision" == "<no value>" ]] && revision=""

stale() {
  echo "::error::${ref} is STALE: built ${1}, but ${last_sha:0:8} (the last commit touching ${image}'s sources) is newer." \
    "Retagging it would ship an image without that change." \
    "Fix: run CI with workflow_dispatch on main (full build), or wait for the in-flight main run to finish, then re-run this job."
  exit 1
}

if [[ -n "$revision" ]]; then
  if ! git cat-file -e "${revision}^{commit}" 2>/dev/null; then
    echo "::error::${ref} revision ${revision} is not in this checkout's history; cannot verify it (fetch-depth: 0?)"
    exit 1
  fi
  if git merge-base --is-ancestor "$last_sha" "$revision"; then
    echo "check-retag-source: ${image} fresh (built from ${revision:0:8}, includes ${last_sha:0:8})"
    exit 0
  fi
  stale "from ${revision:0:8}"
fi

created_epoch=""
# `date -d ""` is today 00:00, so an empty value must not reach it.
[[ -z "$created" ]] || created_epoch="$(date -d "$created" +%s 2>/dev/null || true)"
if [[ -z "$created_epoch" ]]; then
  echo "::error::${ref} has no ${REVISION_LABEL} label and no readable creation time ('${created}')"
  exit 1
fi
if ((created_epoch >= last_time)); then
  echo "check-retag-source: ${image} fresh (created ${created}, after ${last_sha:0:8})"
  exit 0
fi
stale "at ${created} (no revision label)"
