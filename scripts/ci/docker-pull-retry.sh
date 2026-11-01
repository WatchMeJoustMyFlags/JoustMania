#!/usr/bin/env bash
# Pre-pull docker images on a CI runner with bounded retries (#2551): a registry TLS
# flake ("bad record MAC") must not fail a contract job or eject a PR from the queue.
# Usage: scripts/ci/docker-pull-retry.sh <image | compose:<service>>...
# `compose:<service>` resolves the image from docker-compose.yml, so the version stays in one place.
# Each image is pulled up to 3 times, each under `timeout 300`, with a short backoff.
# Test seams: PULL_RETRY_SLEEP (default 5 seconds, times the try number), COMPOSE_FILE.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${ROOT}/docker-compose.yml}"
SLEEP="${PULL_RETRY_SLEEP:-5}"

if [[ $# -eq 0 ]]; then
  echo "usage: docker-pull-retry.sh <image | compose:<service>>..." >&2
  exit 2
fi

compose_image() {
  awk -v svc="$1" '
    /^services:/ { in_services = 1; next }
    in_services && /^[^ #]/ { in_services = 0 }
    in_services && $0 ~ "^  " svc ":[[:space:]]*$" { in_svc = 1; next }
    in_svc && /^  [^ #]/ { in_svc = 0 }
    in_svc && /^    image:/ { print $2; exit }
  ' "${COMPOSE_FILE}"
}

images=()
for arg in "$@"; do
  if [[ "${arg}" == compose:* ]]; then
    image="$(compose_image "${arg#compose:}")"
    if [[ -z "${image}" ]]; then
      echo "no image for compose service '${arg#compose:}' in ${COMPOSE_FILE}" >&2
      exit 2
    fi
    images+=("${image}")
  else
    images+=("${arg}")
  fi
done

for image in "${images[@]}"; do
  pulled=0
  for try in 1 2 3; do
    if timeout 300 docker pull -q "${image}" >/dev/null; then
      pulled=1
      break
    fi
    echo "docker pull ${image} failed (try ${try}/3)" >&2
    [[ "${try}" -lt 3 ]] && sleep $((SLEEP * try))
  done
  if [[ "${pulled}" -ne 1 ]]; then
    echo "docker pull ${image} failed after 3 tries" >&2
    exit 1
  fi
  echo "pulled ${image}"
done
