#!/usr/bin/env bash
# Shadow realism characterization: do SHADOW games (mock controllers, synthetic drivers) play like
# REAL games? (#1017, UC-043 A22; the validity bar it feeds is UC-043 A23, FR-203)
#
# Compares real and shadow games that ended in a time window, per kind: session duration, death
# rate, elimination pace, near-death frequency, movement intensity and variance, and the #2479
# characteristics (activity, change, closeness, survival; decide-contract §14.1, sampled from the
# 1 Hz gauges) plus the skill gap the balanced fitness reads. For each metric it prints the
# distance between the kinds (two-sample KS statistic, ratio of medians shadow / real) and a
# verdict: similar, divergent, or insufficient (too few games of a kind; never counts as similar).
#
# READ-ONLY. It sends GET requests to the VictoriaMetrics Prometheus API and (with --jaeger-url)
# Jaeger's query API v3, nothing else: no flag write, no game start, no docker call. The
# game_kind label (primary = real, shadow) separates the kinds. Safe while games are running.
#
# Usage:
#   scripts/agent-shadow-realism.sh [--since 24h | --start EPOCH --end EPOCH] [--mode FFA]
#       [--vm-url URL] [--jaeger-url URL | --no-jaeger] [--min-n 8] [--ks-max 0.3] [--ratio-max 1.5]
#       [--save FILE] [--json]
#   scripts/agent-shadow-realism.sh --dataset FILE [--min-n ..] [--json]   # report a saved dataset
#
#   --since      window ending now: <n>m, <n>h or <n>d (default 24h)
#   --mode       only games of this mode (the game_current_mode `mode` label, e.g. FFA)
#   --vm-url     default $VM_URL or http://localhost/victoria-metrics
#   --jaeger-url default $JAEGER_URL or http://localhost/jaeger/api; adds the near-death cross-check
#                from the player_analytics span events (Jaeger keeps fewer days than VictoriaMetrics)
#   --save FILE  keep the collected dataset (JSON) for a later --dataset report
#
# Method, thresholds and how to read the result: docs/agent-shadow-realism-runbook.md.
# Exit: 0 ok (whatever the verdicts), 2 usage, 3 a query failed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="${HERE}/agent_shadow_realism.py"
TMP=""

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# seconds <n>m|<n>h|<n>d
seconds() {
  [[ "$1" =~ ^([1-9][0-9]*)([mhd])$ ]] || return 1
  local n="${BASH_REMATCH[1]}"
  case "${BASH_REMATCH[2]}" in
    m) echo $((n * 60)) ;;
    h) echo $((n * 3600)) ;;
    d) echo $((n * 86400)) ;;
  esac
}

main() {
  local since="24h" start="" end="" mode="" save="" dataset=""
  local vm="${VM_URL:-http://localhost/victoria-metrics}" jaeger="${JAEGER_URL:-http://localhost/jaeger/api}"
  local report=()
  while (($#)); do
    case "$1" in
      --since) since="${2:?}"; shift 2 ;;
      --start) start="${2:?}"; shift 2 ;;
      --end) end="${2:?}"; shift 2 ;;
      --mode) mode="${2:?}"; shift 2 ;;
      --vm-url) vm="${2:?}"; shift 2 ;;
      --jaeger-url) jaeger="${2:?}"; shift 2 ;;
      --no-jaeger) jaeger=""; shift ;;
      --save) save="${2:?}"; shift 2 ;;
      --dataset) dataset="${2:?}"; shift 2 ;;
      --min-n | --ks-max | --ratio-max) report+=("$1" "${2:?}"); shift 2 ;;
      --json) report+=(--json); shift ;;
      -h | --help) usage ;;
      *) usage ;;
    esac
  done

  if [[ -n "${dataset}" ]]; then
    [[ -r "${dataset}" ]] || { echo "agent-shadow-realism: cannot read ${dataset}" >&2; exit 2; }
    python3 "${TOOL}" report --dataset "${dataset}" "${report[@]}"
    return
  fi

  if [[ -n "${start}" || -n "${end}" ]]; then
    [[ "${start}" =~ ^[0-9]+$ && "${end}" =~ ^[0-9]+$ ]] || usage
  else
    local span
    span="$(seconds "${since}")" || usage
    end="$(date +%s)"
    start=$((end - span))
  fi

  TMP="$(mktemp)"
  trap 'rm -f "${TMP}"' EXIT
  local args=(collect --vm-url "${vm}" --start "${start}" --end "${end}")
  [[ -n "${jaeger}" ]] && args+=(--jaeger-url "${jaeger}")
  [[ -n "${mode}" ]] && args+=(--mode "${mode}")
  python3 "${TOOL}" "${args[@]}" >"${TMP}" || exit $?
  [[ -n "${save}" ]] && cp "${TMP}" "${save}"
  python3 "${TOOL}" report --dataset "${TMP}" "${report[@]}"
}

main "$@"
