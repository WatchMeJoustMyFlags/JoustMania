#!/usr/bin/env bash
# Self-contained tests for scripts/flags.sh (epic #1407, UC-053).
#
# Stubs curl and docker on PATH: the stub curl records each request and answers
# from canned files, so no stack is needed. Runs as part of `make lint`, or:
#   ./scripts/tests/test-flags.sh
#
# UC: UC-053 A1 A5
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/../flags.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/bin"
export PATH="${WORK}/bin:${PATH}"
export FLAG_SERVICE_URL="http://flags.test/flags"
export STUB_LOG="${WORK}/requests.log"
export STUB_STATUS="${WORK}/status"
export DOCKER_LOG="${WORK}/docker.log"

# Stub curl: parses -X, --data, -o and the URL; logs "METHOD URL BODY"; writes
# the canned body to -o and prints the canned status for -w.
cat >"${WORK}/bin/curl" <<'SH'
#!/usr/bin/env bash
method=GET out="" data="" url=""
while (($#)); do
  case "$1" in
    -X) method="$2"; shift 2 ;;
    --data) data="$2"; shift 2 ;;
    -o) out="$2"; shift 2 ;;
    -w|-H|--max-time) shift 2 ;;
    -sS) shift ;;
    *) url="$1"; shift ;;
  esac
done
echo "${method} ${url} ${data}" >>"${STUB_LOG}"
status="$(cat "${STUB_STATUS}" 2>/dev/null || echo 200)"
if [[ "${status}" == 2* ]]; then echo '{"ok":true}' >"${out}"; else echo '{"error":"flag_read_only","detail":"no"}' >"${out}"; fi
printf '%s' "${status}"
SH
cat >"${WORK}/bin/docker" <<'SH'
#!/usr/bin/env bash
echo "$*" >>"${DOCKER_LOG}"
if [[ "$1" == inspect ]]; then echo healthy; fi
SH
chmod +x "${WORK}/bin/curl" "${WORK}/bin/docker"

fail() { echo "FAIL: $*" >&2; exit 1; }
last_request() { tail -n 1 "${STUB_LOG}"; }

# UC-053/A1: set a declared variant with a writer label
"${HELPER}" set agent decision.backend jev --writer stage >/dev/null
[[ "$(last_request)" == 'PUT http://flags.test/flags/agent/decision.backend {"writer": "stage", "variant": "jev"}' ]] \
  || fail "set: $(last_request)"

# UC-053/A1: a JSON value with a TTL
"${HELPER}" set-value agent decision.openai.model '"vendor-model-1"' --ttl 60 >/dev/null
[[ "$(last_request)" == 'PUT http://flags.test/flags/agent/decision.openai.model {"writer": "operator", "value": "vendor-model-1", "ttl_seconds": 60.0}' ]] \
  || fail "set-value: $(last_request)"

# UC-053/A2: persist on request
"${HELPER}" set agent interventions_allowed standard --writer menu --persist >/dev/null
[[ "$(last_request)" == *'"persist": true'* ]] || fail "persist: $(last_request)"

# clear / clear --persist / reset
"${HELPER}" clear agent decision.backend
[[ "$(last_request)" == 'DELETE http://flags.test/flags/agent/decision.backend ' ]] || fail "clear: $(last_request)"
"${HELPER}" clear agent interventions_allowed --persist
[[ "$(last_request)" == 'DELETE http://flags.test/flags/agent/interventions_allowed?persist=true ' ]] || fail "clear persist: $(last_request)"
"${HELPER}" reset agent stage
[[ "$(last_request)" == 'DELETE http://flags.test/flags/agent?writer=stage ' ]] || fail "reset: $(last_request)"

# reads
"${HELPER}" state >/dev/null
[[ "$(last_request)" == 'GET http://flags.test/flags/state ' ]] || fail "state: $(last_request)"
"${HELPER}" get interventions shield_seconds >/dev/null
[[ "$(last_request)" == 'GET http://flags.test/flags/interventions/shield_seconds ' ]] || fail "get: $(last_request)"

# UC-053/A5: an HTTP error exits 1 and prints the service's reason
echo 422 >"${STUB_STATUS}"
if "${HELPER}" set system idle.enabled off 2>"${WORK}/err"; then fail "a 422 must exit non-zero"; fi
grep -q 'flag_read_only' "${WORK}/err" || fail "the reason code is not printed"
rm -f "${STUB_STATUS}"

# NFR-052: planned restart = flag service, wait healthy, then flagd
"${HELPER}" restart >/dev/null
[[ "$(sed -n 1p "${DOCKER_LOG}")" == "restart joustmania-flag-service" ]] || fail "restart order: $(cat "${DOCKER_LOG}")"
[[ "$(tail -n 1 "${DOCKER_LOG}")" == "restart joustmania-flagd" ]] || fail "flagd not restarted last: $(cat "${DOCKER_LOG}")"

# usage errors exit 2
set +e
"${HELPER}" bogus 2>/dev/null
[[ $? -eq 2 ]] || fail "unknown command must exit 2"
set -e

echo "test-flags.sh: OK"
