#!/usr/bin/env bash
# Tests scripts/ci/check-gofmt.sh and that `make pr-gates` (the lint gate) runs
# it and spec-refs-check (#2527). No gate runs; --print-cmd only prints.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="${SCRIPT_DIR}/../ci/check-gofmt.sh"
GATES="${SCRIPT_DIR}/../ci/local-gates.sh"
unset SERVICE PR_GATES_FILES BASE PR_LABELS
command -v gofmt >/dev/null || { echo "SKIP: no gofmt"; exit 0; }
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

mkdir -p "${WORK}/ok" "${WORK}/bad"
printf 'package x\n\nfunc F() {}\n' >"${WORK}/ok/a.go"
printf 'package x\nfunc F( ) {\n}\n' >"${WORK}/bad/a.go"

"${CHECK}" "${WORK}/ok" >/dev/null || { echo "FAIL: formatted tree must pass" >&2; exit 1; }
if out="$("${CHECK}" "${WORK}/bad" 2>&1)"; then echo "FAIL: unformatted tree must fail" >&2; exit 1; fi
grep -q "bad/a.go" <<<"$out" || { echo "FAIL: offending file not listed: $out" >&2; exit 1; }
# the real trees are clean
"${CHECK}" >/dev/null || { echo "FAIL: repo Go trees not gofmt-clean" >&2; exit 1; }

# pr-gates lint gate runs both checks, so a failure of either fails pr-gates
cmd="$(PR_GATES_FILES=/dev/null "${GATES}" --print-cmd lint)"
grep -q "make spec-refs-check" <<<"$cmd" || { echo "FAIL: lint gate lacks spec-refs-check: $cmd" >&2; exit 1; }
grep -q "scripts/ci/check-gofmt.sh" <<<"$cmd" || { echo "FAIL: lint gate lacks check-gofmt: $cmd" >&2; exit 1; }
[[ "$cmd" == *"&&"* ]] || { echo "FAIL: lint gate parts not chained with &&: $cmd" >&2; exit 1; }
echo "ok"
