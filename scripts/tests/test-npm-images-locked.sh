#!/usr/bin/env bash
# UC: none (C-046, #2306): the dashboard image installs npm dependencies from
# package-lock.json with `npm ci`, never `npm install`. Comment lines are ignored.
#
#   ./scripts/tests/test-npm-images-locked.sh [repo root]
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${1:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
DOCKERFILE="${REPO_ROOT}/services/dashboard/Dockerfile"

fail=0
code="$(grep -v '^[[:space:]]*#' "$DOCKERFILE")"

if ! grep -Eq 'npm ci( |$)' <<<"$code"; then
  echo "FAIL: services/dashboard/Dockerfile does not run \`npm ci\`"; fail=1
fi
if grep -Eq 'npm (install|i)( |$)' <<<"$code"; then
  echo "FAIL: services/dashboard/Dockerfile runs \`npm install\` (resolves fresh, ignores the lock)"; fail=1
fi
if ! grep -Eq '^COPY .*package-lock\.json( |$)' <<<"$code"; then
  echo "FAIL: services/dashboard/Dockerfile does not COPY package-lock.json"; fail=1
fi
if grep -Eq 'package-lock\.json\*' <<<"$code"; then
  echo "FAIL: package-lock.json is an optional glob (a missing lock would pass the COPY)"; fail=1
fi

[ "$fail" -eq 0 ] && echo "PASS: dashboard image installs from the lock (npm ci)"
exit "$fail"
