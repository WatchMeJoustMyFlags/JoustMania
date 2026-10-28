#!/usr/bin/env bash
# `make dry-run` brings the rehearsal / fallback stack up with experiments off
# (UC-070 Pre-1), without starting the stack.
#
# 1. `make -n dry-run` (prints the recipe, runs nothing) layers the CI flag files
#    (docker-compose.ci.yml) under the dry-run override and switches nothing on:
#    the enable helper only appears in echoed hints.
# 2. The dry-run override does not replace the flag baseline mount, so flagd and
#    the flag service serve services/flagd/ci/.
# 3. In that baseline the live gate experiments_enabled defaults to off with no
#    targeting rule. It is the only gate (the env gate was removed in #1907), so
#    the experiment loop starts idle.
# Runs as part of `make lint` (needs bash, make, python3), or directly:
#   ./scripts/tests/test-dry-run-experiments-off.sh
# UC: UC-070 Pre-1
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
pass=0
fail=0
ok() { echo "ok   - $1"; pass=$((pass + 1)); }
not_ok() { echo "FAIL - $1"; fail=$((fail + 1)); }

# --- 1. the recipe ---------------------------------------------------------
recipe="$(make -s -n -C "${ROOT}" dry-run)"
# Commands only: drop the echoed hints (make -n prints them as `echo ...`); a
# hint that names a compose file must not count as layering it.
commands="$(grep -v '^[[:space:]]*echo ' <<<"${recipe}" || true)"
for file in docker-compose.yml docker-compose.ci.yml docker-compose.dry-run.yml; do
  if grep -qF -- "-f ${file}" <<<"${commands}"; then ok "dry-run layers ${file}"; else not_ok "dry-run does not layer ${file}"; fi
done
if grep -Eq 'agent-dryrun-enable\.sh|agent-killswitch\.sh|scripts/flags\.sh' <<<"${commands}"; then
  not_ok "dry-run switches flags at bring-up: ${commands}"
else
  ok "dry-run switches no flag at bring-up"
fi

# --- 2. the flag baseline mount --------------------------------------------
if grep -q '\./services/flagd/ci:/etc/flagd' "${ROOT}/docker-compose.ci.yml"; then
  ok "the CI layer mounts services/flagd/ci as the flag baseline"
else
  not_ok "docker-compose.ci.yml no longer mounts services/flagd/ci"
fi
if grep -Eq '\./services/flagd[^ ]*:/etc/flagd' "${ROOT}/docker-compose.dry-run.yml"; then
  not_ok "docker-compose.dry-run.yml replaces the flag baseline mount"
else
  ok "the dry-run layer keeps the CI flag baseline"
fi

# --- 3. experiments_enabled defaults to off --------------------------------
verdict="$(python3 - "${ROOT}/services/flagd/ci/agent.json" <<'PY'
import json, sys
flag = json.load(open(sys.argv[1]))["flags"].get("experiments_enabled")
if flag is None:
    print("experiments_enabled missing")
elif flag.get("variants", {}).get(flag.get("defaultVariant")) is not False:
    print(f"default variant {flag.get('defaultVariant')!r} is not false")
elif flag.get("targeting"):
    print("a targeting rule can turn it on")
else:
    print("off")
PY
)"
if [[ "${verdict}" == "off" ]]; then
  ok "services/flagd/ci experiments_enabled defaults to off"
else
  not_ok "services/flagd/ci experiments_enabled: ${verdict}"
fi

echo
echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
