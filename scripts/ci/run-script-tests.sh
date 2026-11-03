#!/usr/bin/env bash
# Run every scripts/tests/test-*.sh in parallel (#2564). Sequentially they took ~5 min on a CI
# runner and hit the job timeout; the tests are hermetic (own mktemp dirs, dynamic ports).
#
# Each test's output goes to its own file and is printed as a ::group:: after it finishes,
# so a failure keeps its full output. Every test runs to completion (no fail-fast kill, so
# the whole list of failures is visible); the exit status is non-zero if any test failed.
# JOBS overrides the parallelism (default: number of CPUs); SCRIPT_TESTS_DIR the test directory (for its own test).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

shopt -s nullglob
tests=("${SCRIPT_TESTS_DIR:-scripts/tests}"/test-*.sh)
if [[ ${#tests[@]} -eq 0 ]]; then
  echo "::error::no script tests found under scripts/tests/"
  exit 1
fi

out="$(mktemp -d)"
trap 'rm -rf "${out}"' EXIT
jobs_n="${JOBS:-$(nproc 2>/dev/null || echo 2)}"

run_one() {
  local t="$1" out="$2" start rc
  start=$SECONDS
  "${t}" >"${out}/$(basename "${t}").log" 2>&1
  rc=$?
  echo "${rc} $((SECONDS - start))" >"${out}/$(basename "${t}").rc"
}
export -f run_one

printf '%s\0' "${tests[@]}" |
  xargs -0 -I{} -P "${jobs_n}" bash -c 'run_one "$1" "$2"' _ {} "${out}"

status=0
for t in "${tests[@]}"; do
  name="$(basename "${t}")"
  read -r rc secs <"${out}/${name}.rc" || { rc=1; secs=0; }
  echo "::group::${t} (${secs}s)"
  cat "${out}/${name}.log"
  echo "::endgroup::"
  if [[ "${rc}" != 0 ]]; then
    echo "::error file=${t}::${t} failed (exit ${rc})"
    status=1
  fi
done
exit "${status}"
