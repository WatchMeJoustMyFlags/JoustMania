#!/usr/bin/env bash
# Flag service client for operators and stage scripts (epic #1407, UC-053).
#
# Runtime flags are changed ONLY through the flag service's HTTP API
# (docs/specs/services.md §7.2); the flag files in services/flagd/ are the
# read-only baseline (C-033). This helper wraps the API for the shell.
#
# Usage:
#   scripts/flags.sh restart                      planned restart: flag service, then flagd (NFR-052)
#   scripts/flags.sh state                        GET /state (epoch, sync streams, live games)
#   scripts/flags.sh get <set> [<flag>]           GET /<set>[/<flag>]
#   scripts/flags.sh variant <set> <flag>         print the flag's effective default variant
#   scripts/flags.sh set <set> <flag> <variant> [--writer W] [--ttl S] [--persist]
#   scripts/flags.sh set-value <set> <flag> <json> [--writer W] [--ttl S] [--persist]
#   scripts/flags.sh clear <set> <flag> [--persist] clear the stack overlay (or the persisted admin value)
#   scripts/flags.sh reset <set> <writer>         clear every overlay that writer set in the set
#
# Env:
#   FLAG_SERVICE_URL   default http://localhost/flags (envoy route, C-015)
#   FLAGS_WRITER       default writer label (informational, never checked; D11)
#   DOCKER             docker binary (tests stub it)
#
# Exit status: 0 on success; 1 on an HTTP error (the service's JSON error
# {"error","detail"} is printed to stderr); 2 on a usage error.
set -euo pipefail

FLAG_SERVICE_URL="${FLAG_SERVICE_URL:-http://localhost/flags}"
FLAG_SERVICE_URL="${FLAG_SERVICE_URL%/}"
FLAGS_WRITER="${FLAGS_WRITER:-operator}"
DOCKER="${DOCKER:-docker}"
FLAG_SERVICE_CONTAINER="${FLAG_SERVICE_CONTAINER:-joustmania-flag-service}"
FLAGD_CONTAINER="${FLAGD_CONTAINER:-joustmania-flagd}"
RESTART_TIMEOUT="${RESTART_TIMEOUT:-30}"

usage() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# UC-070 BR-001: every flag change goes through the flag service API; no file is edited.
# request METHOD PATH [BODY]: prints the response body; non-2xx -> stderr, exit 1.
request() {
  local method="$1" path="$2" body="${3:-}" out code
  out="$(mktemp)"
  if [[ -n "${body}" ]]; then
    code="$(curl -sS -o "${out}" -w '%{http_code}' -X "${method}" -H 'Content-Type: application/json' \
      --max-time 5 --data "${body}" "${FLAG_SERVICE_URL}${path}")" || { rm -f "${out}"; echo "flags.sh: flag service unreachable at ${FLAG_SERVICE_URL}" >&2; exit 1; }
  else
    code="$(curl -sS -o "${out}" -w '%{http_code}' -X "${method}" --max-time 5 "${FLAG_SERVICE_URL}${path}")" \
      || { rm -f "${out}"; echo "flags.sh: flag service unreachable at ${FLAG_SERVICE_URL}" >&2; exit 1; }
  fi
  if [[ "${code}" != 2* ]]; then
    echo "flags.sh: ${method} ${path} -> HTTP ${code}: $(cat "${out}")" >&2
    rm -f "${out}"
    exit 1
  fi
  cat "${out}"
  rm -f "${out}"
}

# json_body KIND VALUE WRITER TTL PERSIST: builds a PUT body with python3 (safe quoting).
json_body() {
  python3 - "$@" <<'PY'
import json, sys
kind, value, writer, ttl, persist = sys.argv[1:6]
body = {"writer": writer}
if kind == "variant":
    body["variant"] = value
else:
    body["value"] = json.loads(value)
if ttl:
    body["ttl_seconds"] = float(ttl)
if persist == "1":
    body["persist"] = True
print(json.dumps(body))
PY
}

wait_healthy() {
  local container="$1" deadline=$((SECONDS + RESTART_TIMEOUT)) status
  while (( SECONDS < deadline )); do
    status="$("${DOCKER}" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container}" 2>/dev/null || true)"
    if [[ "${status}" == "healthy" ]]; then
      return 0
    fi
    sleep 0.5
  done
  echo "flags.sh: ${container} not healthy after ${RESTART_TIMEOUT}s (last: ${status:-unknown})" >&2
  return 1
}

cmd_restart() {
  # Planned restart (architecture note §5.4): restart the flag service, wait for
  # it to be healthy, then restart flagd so it reconnects at once instead of
  # after its 4-64 s backoff and serves the baseline plus the persisted admin
  # settings within 5 s (NFR-052). Every in-memory overlay and game scope is gone.
  "${DOCKER}" restart "${FLAG_SERVICE_CONTAINER}" >/dev/null
  wait_healthy "${FLAG_SERVICE_CONTAINER}"
  "${DOCKER}" restart "${FLAGD_CONTAINER}" >/dev/null
  echo "flag service and flagd restarted"
}

parse_opts() {
  WRITER="${FLAGS_WRITER}" TTL="" PERSIST=0
  while (($#)); do
    case "$1" in
      --writer) WRITER="${2:?--writer needs a value}"; shift 2 ;;
      --ttl) TTL="${2:?--ttl needs seconds}"; shift 2 ;;
      --persist) PERSIST=1; shift ;;
      *) echo "flags.sh: unknown option $1" >&2; exit 2 ;;
    esac
  done
}

main() {
  (($#)) || usage
  local cmd="$1"
  shift
  case "${cmd}" in
    restart) cmd_restart ;;
    state) request GET /state ;;
    get)
      (($# >= 1)) || usage
      request GET "/$1${2:+/$2}"
      ;;
    variant)
      (($# == 2)) || usage
      request GET "/$1/$2" | python3 -c 'import json, sys; print(json.load(sys.stdin)["effective_default"])'
      ;;
    set | set-value)
      (($# >= 3)) || usage
      local set="$1" flag="$2" value="$3"
      shift 3
      parse_opts "$@"
      local kind=variant
      [[ "${cmd}" == set-value ]] && kind=value
      request PUT "/${set}/${flag}" "$(json_body "${kind}" "${value}" "${WRITER}" "${TTL}" "${PERSIST}")"
      ;;
    clear)
      (($# >= 2)) || usage
      local set="$1" flag="$2" query=""
      shift 2
      parse_opts "$@"
      ((PERSIST)) && query="?persist=true"
      request DELETE "/${set}/${flag}${query}" >/dev/null
      ;;
    reset)
      (($# == 2)) || usage
      request DELETE "/$1?writer=$2" >/dev/null
      ;;
    -h | --help | help) usage ;;
    *) echo "flags.sh: unknown command ${cmd}" >&2; usage ;;
  esac
}

main "$@"
