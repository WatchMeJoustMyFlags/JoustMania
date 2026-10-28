#!/usr/bin/env bash
# Fail any scripts/tests/test-*.sh that does not source scripts/tests/lib/hermetic.sh (#2315).
# Explicit opt-out: a line `# hermetic: opt-out <reason>` (reason required).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
bad=0
for t in scripts/tests/test-*.sh; do
  if grep -qE '^[[:space:]]*(source|\.)[[:space:]].*lib/hermetic\.sh' "$t"; then continue; fi
  if grep -qE '^# hermetic: opt-out .+' "$t"; then continue; fi
  echo "not hermetic: $t does not source scripts/tests/lib/hermetic.sh (or add '# hermetic: opt-out <reason>')" >&2
  bad=1
done
exit "$bad"
