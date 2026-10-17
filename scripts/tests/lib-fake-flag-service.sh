#!/usr/bin/env bash
# Sourced by the script tests: start/stop scripts/tests/fake_flag_service.py.
#
#   start_fake_flag_service <workdir> [extra fake args...]
#       starts the fake over services/flagd/ci/agent.json (+ the real registry),
#       exports FLAG_SERVICE_URL and FAKE_FLAG_LOG; stop_fake_flag_service kills it.

_FAKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT="$(cd "${_FAKE_DIR}/../.." && pwd)"
FAKE_FLAG_PID=""

start_fake_flag_service() {
  local work="$1"
  shift
  local port_file="${work}/fake-port"
  export FAKE_FLAG_LOG="${work}/fake-requests.log"
  : >"${FAKE_FLAG_LOG}"
  rm -f "${port_file}"
  python3 "${_FAKE_DIR}/fake_flag_service.py" \
    --flags "agent=${FAKE_AGENT_FLAGS:-${_REPO_ROOT}/services/flagd/ci/agent.json}" \
    --flags "controller=${_REPO_ROOT}/services/flagd/ci/controller.json" \
    --registry "${_REPO_ROOT}/services/flag-service/registry.yaml" \
    --port-file "${port_file}" --log "${FAKE_FLAG_LOG}" "$@" &
  FAKE_FLAG_PID=$!
  local i
  for i in $(seq 1 100); do
    [[ -s "${port_file}" ]] && break
    sleep 0.05
  done
  [[ -s "${port_file}" ]] || { echo "fake flag service did not start" >&2; return 1; }
  export FLAG_SERVICE_URL="http://127.0.0.1:$(cat "${port_file}")"
}

stop_fake_flag_service() {
  if [[ -n "${FAKE_FLAG_PID}" ]]; then
    kill "${FAKE_FLAG_PID}" 2>/dev/null || true
    wait "${FAKE_FLAG_PID}" 2>/dev/null || true
    FAKE_FLAG_PID=""
  fi
}
