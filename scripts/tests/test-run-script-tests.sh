#!/usr/bin/env bash
# UC: none (CI tooling)
# Tests scripts/ci/run-script-tests.sh (#2564): the parallel runner keeps per-test output, runs
# every test and fails the job when any test fails.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/hermetic.sh"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="${ROOT}/scripts/ci/run-script-tests.sh"
W="$(mktemp -d)"; trap 'rm -rf "${W}"' EXIT
fails=0
ok() { echo "ok   $1"; }
bad() { echo "FAIL $1"; fails=$((fails + 1)); }

mkdir "${W}/pass" "${W}/mixed" "${W}/empty"
printf '#!/usr/bin/env bash\necho hello-from-a\n' >"${W}/pass/test-a.sh"
printf '#!/usr/bin/env bash\nsleep 1; echo hello-from-b\n' >"${W}/pass/test-b.sh"
cp "${W}/pass/"*.sh "${W}/mixed/"
printf '#!/usr/bin/env bash\necho boom-output\nexit 3\n' >"${W}/mixed/test-c.sh"
chmod +x "${W}"/*/test-*.sh

out="$(SCRIPT_TESTS_DIR="${W}/pass" "${RUNNER}" 2>&1)"; rc=$?
[[ ${rc} -eq 0 ]] && ok "all passing -> exit 0" || bad "all passing -> exit 0 (rc=${rc})"
[[ "${out}" == *hello-from-a* && "${out}" == *hello-from-b* ]] && ok "output of each test shown" || bad "output of each test shown"

out="$(SCRIPT_TESTS_DIR="${W}/mixed" "${RUNNER}" 2>&1)"; rc=$?
[[ ${rc} -ne 0 ]] && ok "one failing test -> non-zero exit" || bad "one failing test -> non-zero exit"
[[ "${out}" == *boom-output* && "${out}" == *"::error"*test-c.sh* ]] && ok "failing output + error annotation shown" || bad "failing output + error annotation shown"
[[ "${out}" == *hello-from-b* ]] && ok "other tests still run" || bad "other tests still run"

SCRIPT_TESTS_DIR="${W}/empty" "${RUNNER}" >/dev/null 2>&1 && bad "no tests -> fail" || ok "no tests -> fail"

[[ ${fails} -eq 0 ]]
