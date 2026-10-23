#!/usr/bin/env bash
# Hermetic test: scripts/agent-backend.sh talks to the flag service through curl
# (like scripts/flags.sh) and tells a non-flag-service HTTP answer apart from a
# genuinely missing flag (#2039). A fake curl answers; no server, no docker.
#
# Runs as part of `make lint`, or directly: ./scripts/tests/test-agent-backend-transport.sh
#
# UC: UC-044 A4
set -euo pipefail
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
printf '%s\n%s' "${FAKE_CURL_BODY}" "${FAKE_CURL_CODE}"
CURL
chmod +x "${WORK}/bin/curl" "${WORK}/bin/docker"
export PATH="${WORK}/bin:${PATH}"
export FAKE_CURL_LOG="${WORK}/curl.log"
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
if grep -qx 'http://localhost/flags/agent/decision.jev.model' "${FAKE_CURL_LOG}"; then
  echo "ok - requests go through curl to FLAG_SERVICE_URL as given"; pass=$((pass + 1))
else
  echo "not ok - curl was not called with the FLAG_SERVICE_URL host ($(cat "${FAKE_CURL_LOG}"))"; fail=$((fail + 1))
fi
echo; echo "${pass} passed, ${fail} failed"
[[ "${fail}" -eq 0 ]]
