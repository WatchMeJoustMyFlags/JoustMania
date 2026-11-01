#!/usr/bin/env bash
# Operator A/B over one-shot SHADOW games: does the LLM come up with group shields on its
# own, and does the neutral prompt profile change that? (#2467 PR 6, UC-040 A47, BR-033, BR-036)
#
# Arms (group shields are on in both; only the wording differs):
#   A  decision.prompt_profile = v1       the shipped prompt
#   B  decision.prompt_profile = neutral  levers described by their effects
# The script never decides when a group shield is right; it only offers the lever
# (interventions_allowed=shadow_group, policy.group_grant.enabled=on) and counts what the
# model does. Real games are never touched: policy.group_grant.real_games is not set here.
#
# Usage:
#   scripts/agent-group-ab.sh run --archetype <name> [--seeds 1,2,3] [--reps 2] [--backend jev] [--out DIR]
#   scripts/agent-group-ab.sh report --out DIR [--jaeger-url http://localhost/jaeger/api] [--json]
#   scripts/agent-group-ab.sh restore --out DIR     put every flag back from DIR/snapshot.tsv (manual recovery)
#
# run:
#   1. refuses while the flag service lists a real game (GET /games; an unknown kind counts as real);
#   2. snapshots the stack overlay of every flag it touches (DIR/snapshot.tsv);
#   3. stops the stack's agent container (it would decide in the one-shot game too);
#   4. sets the preconditions, then runs the seed-matched games A,B,B,A per seed (reps 2);
#      before every game it checks for a real game again and, if one appeared, restores and aborts;
#   5. on exit, error, SIGINT or SIGTERM: restores every flag and restarts the agent container.
# Each finished game appends one line to DIR/manifest.jsonl (arm, profile, seed, game_id, replay record).
#
# report: per arm, from Jaeger, only games whose remote agent.decide spans carry the arm's
# decide.prompt_profile: group and single grants, their timing in the game, the outcomes.
#
# The llm budget is global: run only with no real game live. The cost of a run is in
# docs/agent-group-ab-runbook.md (pre-registration, grid, success criterion).
#
# Env: FLAGS_SH (default scripts/flags.sh), FLAG_SERVICE_URL (as there), COMPOSE (default
# "docker compose"), DOCKER (default docker), AB_AGENT_SERVICE (default agent),
# PATTERN_REPLAY_TIMEOUT (seconds per game, default from scripts/pattern-replay-check.sh).
# Exit: 0 ok, 2 usage, 3 run error, 4 refused or aborted because a real game is live.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLAGS_SH="${FLAGS_SH:-${HERE}/flags.sh}"
TOOL="${HERE}/agent_group_ab.py"
FLAG_SERVICE_URL="${FLAG_SERVICE_URL:-http://localhost/flags}"
FLAG_SERVICE_URL="${FLAG_SERVICE_URL%/}"
DOCKER="${DOCKER:-docker}"
COMPOSE="${COMPOSE:-docker compose}"
AGENT_SERVICE="${AB_AGENT_SERVICE:-agent}"
WRITER="group-ab"
# Flags the run writes; the snapshot and the restore cover exactly these.
TOUCHED=(decision.prompt_profile decision.backend interventions_allowed llm.eligible_game_kinds
  experiment_dynamic_enabled policy.group_grant.enabled)

usage() {
  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

OUT="" AGENT_WAS_RUNNING=0 CLEANUP_ARMED=0 SNAPSHOT_TAKEN=0

flag_view() { "${FLAGS_SH}" get agent "$1"; }

# real_game_ids: ids of live games that are not shadow games; fails closed (exit 3) if unreadable.
real_game_ids() {
  local body
  body="$(curl -sS --max-time 5 "${FLAG_SERVICE_URL}/games")" || {
    echo "agent-group-ab: cannot read ${FLAG_SERVICE_URL}/games; refusing (fail closed)" >&2
    return 3
  }
  python3 "${TOOL}" real-games <<<"${body}"
}

snapshot_flags() {
  : >"${OUT}/snapshot.tsv"
  local flag view
  for flag in "${TOUCHED[@]}"; do
    view="$(flag_view "${flag}")" || return 3
    printf '%s\t%s\n' "${flag}" "$(python3 "${TOOL}" overlay <<<"${view}")" >>"${OUT}/snapshot.tsv"
  done
  SNAPSHOT_TAKEN=1
}

# restore_flags FILE: clear the overlay we wrote, or put the previous overlay back.
restore_flags() {
  local file="$1" flag prev rc=0
  [[ -s "${file}" ]] || { echo "agent-group-ab: no snapshot at ${file}" >&2; return 3; }
  while IFS=$'\t' read -r flag prev; do
    case "${prev}" in
      none) "${FLAGS_SH}" clear agent "${flag}" --writer "${WRITER}" || rc=3 ;;
      variant:*) "${FLAGS_SH}" set agent "${flag}" "${prev#variant:}" --writer "${WRITER}" || rc=3 ;;
      value:*) "${FLAGS_SH}" set-value agent "${flag}" "${prev#value:}" --writer "${WRITER}" || rc=3 ;;
      *) echo "agent-group-ab: bad snapshot line for ${flag}" >&2; rc=3 ;;
    esac
  done <"${file}"
  return "${rc}"
}

cleanup() {
  local code=$?
  trap - EXIT INT TERM
  if ((CLEANUP_ARMED)); then
    if ((SNAPSHOT_TAKEN)); then
      restore_flags "${OUT}/snapshot.tsv" || echo "agent-group-ab: restore failed; run: $0 restore --out ${OUT}" >&2
    fi
    if ((AGENT_WAS_RUNNING)); then
      # shellcheck disable=SC2086 # COMPOSE may hold two words
      ${COMPOSE} start "${AGENT_SERVICE}" >/dev/null 2>&1 || echo "agent-group-ab: could not restart ${AGENT_SERVICE}" >&2
    fi
  fi
  exit "${code}"
}

arm_profile() { case "$1" in A) echo v1 ;; B) echo neutral ;; esac; }

# run_game ARM SEED REP ARCHETYPE: one one-shot shadow game; appends the manifest line.
run_game() {
  local arm="$1" seed="$2" rep="$3" archetype="$4" profile name line="" deadline timeout
  profile="$(arm_profile "${arm}")"
  local live
  live="$(real_game_ids)" || return 3
  if [[ -n "${live}" ]]; then
    echo "agent-group-ab: a real game is live (${live//$'\n'/ }); restoring the flags and aborting" >&2
    return 4
  fi
  "${FLAGS_SH}" set agent decision.prompt_profile "${profile}" --writer "${WRITER}" >/dev/null
  name="group-ab-$$-${arm}-${seed}-${rep}"
  # shellcheck disable=SC2086
  ${COMPOSE} run -d --no-deps --name "${name}" -e AGENT_SHADOW_GAME=true -e SHADOW_GAME_MODE=JoustFFA \
    -e "SHADOW_GAME_ARCHETYPE=${archetype}" -e "SHADOW_GAME_SEED=${seed}" "${AGENT_SERVICE}" >/dev/null
  timeout="$("${HERE}/pattern-replay-check.sh" --show-timeout)"
  deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    line="$("${DOCKER}" logs "${name}" 2>/dev/null | grep -m1 '^PATTERN_REPLAY_RECORD ' || true)"
    [[ -n "${line}" ]] && break
    sleep 1
  done
  "${DOCKER}" rm -f "${name}" >/dev/null 2>&1 || true
  if [[ -z "${line}" ]]; then
    echo "agent-group-ab: game ${arm} seed ${seed} rep ${rep}: no PATTERN_REPLAY_RECORD within ${timeout}s" >&2
    return 3
  fi
  printf '%s\n' "${line#PATTERN_REPLAY_RECORD }" >"${OUT}/record.tmp"
  python3 "${TOOL}" manifest-line --arm "${arm}" --profile "${profile}" --seed "${seed}" --rep "${rep}" \
    --archetype "${archetype}" --record-file "${OUT}/record.tmp" >>"${OUT}/manifest.jsonl"
  echo "game ${arm} (${profile}) seed ${seed} rep ${rep}: done"
}

cmd_run() {
  local archetype="" seeds="1,2,3" reps=2 backend="jev"
  while (($#)); do
    case "$1" in
      --archetype) archetype="${2:?}"; shift 2 ;;
      --seeds) seeds="${2:?}"; shift 2 ;;
      --reps) reps="${2:?}"; shift 2 ;;
      --backend) backend="${2:?}"; shift 2 ;;
      --out) OUT="${2:?}"; shift 2 ;;
      *) usage ;;
    esac
  done
  [[ -n "${archetype}" && -n "${OUT}" ]] || usage
  [[ "${reps}" =~ ^[1-9][0-9]*$ ]] || usage
  mkdir -p "${OUT}"

  local live
  live="$(real_game_ids)" || exit 3
  if [[ -n "${live}" ]]; then
    echo "agent-group-ab: refusing to start: a real game is live (${live//$'\n'/ })" >&2
    exit 4
  fi

  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  CLEANUP_ARMED=1
  snapshot_flags || exit 3
  if [[ -n "$(${COMPOSE} ps --status running -q "${AGENT_SERVICE}" 2>/dev/null || true)" ]]; then
    AGENT_WAS_RUNNING=1
    # shellcheck disable=SC2086
    ${COMPOSE} stop "${AGENT_SERVICE}" >/dev/null
  fi

  local set_flag
  set_flag() { "${FLAGS_SH}" set agent "$1" "$2" --writer "${WRITER}" >/dev/null; }
  set_flag experiment_dynamic_enabled off
  set_flag llm.eligible_game_kinds real_and_shadow
  set_flag interventions_allowed shadow_group
  set_flag policy.group_grant.enabled on
  set_flag decision.backend "${backend}"
  echo "real_games gate (policy.group_grant.real_games, untouched): $("${FLAGS_SH}" variant agent policy.group_grant.real_games 2>/dev/null || echo unknown)"

  : >"${OUT}/manifest.jsonl"
  local seed rep order arm
  IFS=',' read -r -a seed_list <<<"${seeds}"
  for seed in "${seed_list[@]}"; do
    for ((rep = 1; rep <= reps; rep++)); do
      # A,B then B,A: the order alternates per repetition.
      if ((rep % 2 == 1)); then order="A B"; else order="B A"; fi
      for arm in ${order}; do
        run_game "${arm}" "${seed}" "${rep}" "${archetype}" || exit $?
      done
    done
  done
  echo "run finished: scripts/agent-group-ab.sh report --out ${OUT}"
}

cmd_report() {
  local jaeger="http://localhost/jaeger/api" json=() traces=()
  while (($#)); do
    case "$1" in
      --out) OUT="${2:?}"; shift 2 ;;
      --jaeger-url) jaeger="${2:?}"; shift 2 ;;
      --traces-file) traces=(--traces-file "${2:?}"); shift 2 ;;
      --json) json=(--json); shift ;;
      *) usage ;;
    esac
  done
  [[ -n "${OUT}" ]] || usage
  if ((${#traces[@]})); then
    python3 "${TOOL}" report --manifest "${OUT}/manifest.jsonl" "${traces[@]}" "${json[@]}"
  else
    python3 "${TOOL}" report --manifest "${OUT}/manifest.jsonl" --jaeger-url "${jaeger}" "${json[@]}"
  fi
}

cmd_restore() {
  [[ "${1:-}" == "--out" && -n "${2:-}" ]] || usage
  restore_flags "$2/snapshot.tsv"
}

main() {
  (($#)) || usage
  local cmd="$1"
  shift
  case "${cmd}" in
    run) cmd_run "$@" ;;
    report) cmd_report "$@" ;;
    restore) cmd_restore "$@" ;;
    *) usage ;;
  esac
}

main "$@"
