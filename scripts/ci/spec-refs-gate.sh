#!/usr/bin/env bash
# Spec references gate (#1280): `make spec-refs-check` plus the comparison with
# the base commit (line-ref growth warning, baseline growth needs the
# `spec-baseline-growth` label). Used by the Telemetry Contract job.
#
# Env: BASE_REF (PR: base branch name) or BASE_SHA (merge queue: base commit),
# PR_NUMBER + GH_TOKEN + GH_REPO (label lookup), PR_LABELS (local testing).
# Neither base set: only the checks that need no base run.
set -euo pipefail

base_arg=()
target="${BASE_SHA:-${BASE_REF:-}}"
if [[ -n "$target" ]]; then
  fetch=(git fetch --no-tags)
  # Only a clone that is already shallow (the CI checkout) gets a depth fetch.
  if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
    fetch+=(--depth=1)
  fi
  for attempt in 1 2 3; do
    if "${fetch[@]}" origin "$target" >/dev/null 2>&1; then
      base_arg=(--base FETCH_HEAD)
      break
    fi
    sleep $((attempt * 2))
  done
  if [[ ${#base_arg[@]} -eq 0 ]]; then
    echo "::warning::Could not fetch $target; base comparison skipped."
  fi
fi
exec make spec-refs-check SPEC_REFS_ARGS="${base_arg[*]}"
