#!/usr/bin/env bash
# Hermetic test: scripts/agent-backend.sh talks to the flag service through curl
# (like scripts/flags.sh) and tells a non-flag-service HTTP answer apart from a
# genuinely missing flag (#2039). A fake curl answers; no server, no docker.
#
# Runs as part of `make lint`, or directly: ./scripts/tests/test-agent-backend-transport.sh
#
# UC: UC-044 A4
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"
unset COMPOSE DOCKER
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/../agent-backend.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
mkdir "${WORK}/bin"
cp "${SCRIPT_DIR}/lib/fail-docker/docker" "${WORK}/bin/docker"
cat >"${WORK}/bin/curl" <<'CURL'
#!/usr/bin/env bash
# Fake curl: logs the URL (last arg), prints $FAKE_CURL_BODY then "\n<code>" (the -w format).
echo "${@: -1}" >>"${FAKE_CURL_LOG}"
echo "$*" >>"${FAKE_CURL_ARGS}"
if [[ -n "${FAKE_CURL_EXIT:-}" ]]; then
  echo "curl: (28) Operation timed out" >&2
  exit "${FAKE_CURL_EXIT}"
fi
printf '%s\n%s' "${FAKE_CURL_BODY}" "${FAKE_CURL_CODE}"
CURL
chmod +x "${WORK}/bin/curl" "${WORK}/bin/docker"
export PATH="${WORK}/bin:${PATH}"
export FAKE_CURL_LOG="${WORK}/curl.log"
export FAKE_CURL_ARGS="${WORK}/curl.args"
export FLAG_SERVICE_URL="http://localhost/flags"

pass=0 fail=0
check() { # name, want_rc, want_substring, [forbidden_substring] -- args
  local name="$1" want_rc="$2" want="$3" forbid="$4" out rc=0
  shift 5
  out="$("${HELPER}" "$@" 2>&1)" || rc=$?
  if [[ "${rc}" -eq "${want_rc}" && "${out}" == *"${want}"* && ( -z "${forbid}" || "${out}" != *"${forbid}"* ) ]]; then
    echo "ok - ${name}"; pass=$((pass + 1))
  else
    echo "not ok - ${name} (rc=${rc}; output: ${out})"; fail=$((fail + 1))
  fi
}

FAKE_CURL_CODE=404 FAKE_CURL_BODY='<html>404 Not Found</html>' \
  check "A4 a 404 from another server is not 'flag not defined'" 1 "is FLAG_SERVICE_URL the flag service" "not defined" -- jev small
FAKE_CURL_CODE=404 FAKE_CURL_BODY='{"error":"unknown_flag","detail":"decision.jev.model"}' \
  check "A4 a real missing flag still says 'not defined'" 1 "flag 'decision.jev.model' not defined" "" -- jev small
FAKE_CURL_CODE=404 FAKE_CURL_BODY='{"error":"boom","detail":"something else"}' \
  check "A4 a JSON 404 with another error is a clear message, no traceback" 1 "flag service: boom: something else" "Traceback" -- jev small
FAKE_CURL_CODE=200 FAKE_CURL_BODY='<html>welcome</html>' \
  check "A4 a 2xx non-JSON answer names the URL hint, no traceback" 1 "is FLAG_SERVICE_URL the flag service" "Traceback" -- jev small
FAKE_CURL_CODE=000 FAKE_CURL_BODY='' FAKE_CURL_EXIT=28 \
  check "A4 a curl timeout reports the flag service unreachable" 1 "flag service unreachable at http://localhost/flags" "Traceback" -- jev small
if grep -q -- ' -- http://localhost/flags/' "${FAKE_CURL_ARGS}"; then
  echo "ok - the URL follows -- in the curl argv"; pass=$((pass + 1))
else
  echo "not ok - URL not after -- ($(head -1 "${FAKE_CURL_ARGS}"))"; fail=$((fail + 1))
fi
if grep -qx 'http://localhost/flags/agent/decision.jev.model' "${FAKE_CURL_LOG}"; then
  echo "ok - requests go through curl to FLAG_SERVICE_URL as given"; pass=$((pass + 1))
else
  echo "not ok - curl was not called with the FLAG_SERVICE_URL host ($(cat "${FAKE_CURL_LOG}"))"; fail=$((fail + 1))
fi

# --- scripts/flagservice.py: curl is the default transport for every caller (#2057) ---
PY="$(command -v python3)"
: >"${FAKE_CURL_LOG}"
transport() { # use_curl arg (None|True|False) -> prints use_curl, then the number of curl calls
  FAKE_CURL_CODE=200 FAKE_CURL_BODY='{"flags":[]}' "$@" "${PY}" -c '
import sys
sys.path.insert(0, sys.argv[1])
from flagservice import FlagService
fs = FlagService.from_env(use_curl={"None": None, "True": True, "False": False}[sys.argv[2]])
print(fs.use_curl)
try:
    fs.state()
except Exception:
    pass
' "${SCRIPT_DIR}/.." "${ARG}"
}
ARG=None; got="$(transport env)"
[[ "${got}" == "True" ]] && { echo "ok - default transport is curl"; pass=$((pass + 1)); } \
  || { echo "not ok - default transport is not curl (${got})"; fail=$((fail + 1)); }
[[ "$(wc -l <"${FAKE_CURL_LOG}")" -eq 1 ]] && { echo "ok - the default request went through curl"; pass=$((pass + 1)); } \
  || { echo "not ok - default request did not call curl once"; fail=$((fail + 1)); }
ARG=False; got="$(transport env)"
[[ "${got}" == "False" ]] && { echo "ok - use_curl=False keeps urllib"; pass=$((pass + 1)); } \
  || { echo "not ok - use_curl=False was not honoured (${got})"; fail=$((fail + 1)); }
ARG=None; got="$(transport env PATH="$(dirname "${PY}")")"
if [[ ! -x "$(dirname "${PY}")/curl" ]]; then
  [[ "${got}" == "False" ]] && { echo "ok - without curl the default falls back to urllib"; pass=$((pass + 1)); } \
    || { echo "not ok - no curl on PATH but use_curl=${got}"; fail=$((fail + 1)); }
fi
echo; echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
