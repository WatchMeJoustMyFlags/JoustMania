#!/usr/bin/env bash
# Install apt packages on a CI runner without hanging on a dead mirror (#2494).
# Usage: scripts/ci/apt-install.sh [--no-install-recommends] <package>...
# `apt-get update` is tried 3 times, each under `timeout 120`; install under `timeout 300`.
# Test seams: APT_SUDO (default sudo), APT_RETRY_SLEEP (default 5).
set -euo pipefail

SUDO="${APT_SUDO-sudo}"
SLEEP="${APT_RETRY_SLEEP:-5}"
APT_OPTS=(-o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)

extra=()
if [[ "${1:-}" == "--no-install-recommends" ]]; then
  extra+=(--no-install-recommends)
  shift
fi
if [[ $# -eq 0 ]]; then
  echo "usage: apt-install.sh [--no-install-recommends] <package>..." >&2
  exit 2
fi

updated=0
for try in 1 2 3; do
  if timeout 120 ${SUDO} apt-get "${APT_OPTS[@]}" update -q; then
    updated=1
    break
  fi
  echo "apt-get update failed (try ${try}/3)" >&2
  [[ "${try}" -lt 3 ]] && sleep "${SLEEP}"
done
if [[ "${updated}" -ne 1 ]]; then
  echo "apt-get update failed after 3 tries" >&2
  exit 1
fi

timeout 300 ${SUDO} apt-get "${APT_OPTS[@]}" install -y -q "${extra[@]}" "$@"
