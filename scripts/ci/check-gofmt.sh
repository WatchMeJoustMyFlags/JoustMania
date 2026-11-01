#!/usr/bin/env bash
# Fail when gofmt -l lists any file under the Go service trees (#2527).
# Usage: check-gofmt.sh [dir ...]   (default: services/agent services/flag-service)
# Runs from the repo root regardless of the caller's directory; no docker.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 2
if ! command -v gofmt >/dev/null 2>&1; then
  echo "check-gofmt: gofmt not found on PATH" >&2
  exit 2
fi
dirs=("$@")
[[ ${#dirs[@]} -gt 0 ]] || dirs=(services/agent services/flag-service)
out="$(gofmt -l "${dirs[@]}")" || exit 2
if [[ -n "$out" ]]; then
  echo "check-gofmt: unformatted Go files (run gofmt -w):" >&2
  echo "$out" >&2
  exit 1
fi
echo "check-gofmt: ok (${dirs[*]})"
