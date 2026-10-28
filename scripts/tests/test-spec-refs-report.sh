#!/usr/bin/env bash
# Tests for `scripts/spec_refs.py --report` (#1918): broken and drifted line refs are
# listed, a clean tree reports 0, and the exit code is always 0. Scratch git repo.
# UC: none (CI-only)
#
#   ./scripts/tests/test-spec-refs-report.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
fail() { echo "FAILED: $*" >&2; exit 1; }

repo="${WORK}/repo"
mkdir -p "${repo}/scripts" "${repo}/tests/contract" "${repo}/docs/specs" "${repo}/lib"
cp "${REPO_ROOT}/scripts/spec_refs.py" "${repo}/scripts/"
cp "${REPO_ROOT}/tests/contract/telemetry_inventory.py" "${repo}/tests/contract/"
cd "$repo"
git init -q
git config user.email t@example.invalid
git config user.name t
printf 'def a():\n    return 1\n\ndef b():\n    return 2\n' > lib/code.py
cat > docs/specs/x.md <<'MD'
Row one cites `lib/code.py:5` and a gone file `lib/gone.py:3`.
MD
git add -A && git commit -qm base

report() { python3 scripts/spec_refs.py --report; }

out="$(report)" || fail "report exited non-zero on a clean tree"
head -n1 <<<"$out" | grep -q 'spec-refs-drift: 1 -->' \
  || fail "expected 1 finding (the gone file), got: $(head -n1 <<<"$out")"
grep -q 'lib/gone.py' <<<"$out" || fail "broken ref not listed"
grep -q 'Possibly drifted' <<<"$out" && fail "no drift expected yet"

# change the cited line after the spec line: drift
printf 'def a():\n    return 1\n\ndef b():\n    return 3\n' > lib/code.py
git commit -qam "code change"
out="$(report)" || fail "report exited non-zero with drift"
grep -q 'spec-refs-drift: 2 -->' <<<"$out" || fail "expected 2 findings, got: $(head -n1 <<<"$out")"
grep -q 'cites `lib/code.py:5`' <<<"$out" || fail "drifted ref not listed"
grep -q 'lib/code.py:b' <<<"$out" || fail "symbol suggestion missing"

# fix both: clean report, still exit 0
cat > docs/specs/x.md <<'MD'
Row one cites `lib/code.py:b`.
MD
git commit -qam "spec fix"
out="$(report)" || fail "report exited non-zero when clean"
grep -q 'spec-refs-drift: 0 -->' <<<"$out" || fail "expected 0 findings, got: $out"

echo "test-spec-refs-report: all passed"
