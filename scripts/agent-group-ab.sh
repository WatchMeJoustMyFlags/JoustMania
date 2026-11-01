#!/usr/bin/env bash
# Operator A/B over one-shot SHADOW games: does the LLM come up with group shields on its
# own, and does the neutral prompt profile change that? (#2467 PR 6, UC-040 A47, BR-033, BR-036)
#
# Arms (group shields are on in both; only the wording differs; a third arm, v1 without the
# lever, is not implemented):
#   A  decision.prompt_profile = v1       the shipped prompt
#   B  decision.prompt_profile = neutral  levers described by their effects
# The script never decides when a group shield is right; it only offers the lever and counts
# what the model does.
#
# THE FLAGS ARE GLOBAL. Every flag below is registered with scope [stack] only
# (services/flag-service/registry.yaml), so none can be limited to shadow games. While a run is
# up, real games see them too. The protection of real games is the guard, not the scope:
# the script refuses to start, and polls every 3 s during each game, while a real game is live
# or policy.group_grant.real_games is not off. On a violation it aborts within one poll (3 s) and restores.
# A game that starts in the seconds before a poll sees the experiment flags.
#
# Usage:
#   scripts/agent-group-ab.sh run --archetype <name> [--seeds 1,2,3] [--reps 2] [--backend jev] --out DIR
#       [--use-aliases] [--no-verify] [--jaeger-url URL | --traces-file F]
#   scripts/agent-group-ab.sh report --out DIR [--jaeger-url http://localhost/jaeger/api] [--json]
#   scripts/agent-group-ab.sh restore --out DIR
#
# run:
#   1. refuses to start when a real game is live, real_games is not off, a persisted
#      interventions_allowed=none (the admin kill switch) would be lifted, DIR/snapshot.tsv exists
#      (a killed run) or a touched flag already carries a group-ab overlay;
#   2. snapshots the stack overlay of the six flags it writes, with writer and expiry
#      (DIR/snapshot.tsv): decision.prompt_profile, decision.backend, interventions_allowed,
#      llm.eligible_game_kinds, experiment_dynamic_enabled, policy.group_grant.enabled;
#   3. stops the stack's agent container (it would decide in the one-shot game too; marker
#      DIR/agent-was-running);
#   4. runs the seed-matched games A,B,B,A per seed (reps 2); the first game is verified: its
#      agent.decide spans must carry the arm's profile, or the run stops;
#   5. on exit, error, SIGINT or SIGTERM: restores each flag (same writer, remaining TTL) only
#      when its overlay is still the group-ab one, removes group-ab-* containers and starts the agent.
# SIGHUP (a lost terminal) restores like SIGTERM and exits 129.
# SIGKILL skips the trap: run `restore --out DIR` (flags, containers, agent).
# Each finished game appends one line to DIR/manifest.jsonl.
#
# report: per arm, from Jaeger, only games whose remote agent.decide spans carry the arm's
# decide.prompt_profile: group and single grants, timing in the game, outcomes, paired by seed.
#
# The llm budget is global: run only with no real game live. Design and cost:
# docs/agent-group-ab-runbook.md.
#
# Env: FLAGS_SH (default scripts/flags.sh), FLAG_SERVICE_URL (as there), COMPOSE (default
# "docker compose"), DOCKER (default docker), AB_AGENT_SERVICE (default agent),
# PATTERN_REPLAY_TIMEOUT (seconds per game, default from scripts/pattern-replay-check.sh),
# AB_VERIFY_TRIES / AB_VERIFY_WAIT (first-game verification, default 8 tries 3 s apart).
# Exit: 0 ok, 2 usage, 3 run error, 4 refused or aborted because of a real game.
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
CONTAINER_PREFIX="group-ab-"
# Flags the run writes; the snapshot and the restore cover exactly these.
TOUCHED=(decision.prompt_profile decision.backend interventions_allowed llm.eligible_game_kinds
  experiment_dynamic_enabled policy.group_grant.enabled)

usage() {
  sed -n '2,51p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

OUT="" CLEANUP_ARMED=0

flag_view() { "${FLAGS_SH}" get agent "$1"; }
tool() { python3 "${TOOL}" "$@"; }

# real_game_ids: ids of live games that are not shadow games; fails closed (exit 3) if unreadable.
real_game_ids() {
  local body
  body="$(curl -sS --max-time 5 "${FLAG_SERVICE_URL}/games")" || {
    echo "agent-group-ab: cannot read ${FLAG_SERVICE_URL}/games; refusing (fail closed)" >&2
    return 3
  }
  tool real-games <<<"${body}"
}

# real_guard: 0 only when no real game is live AND policy.group_grant.real_games is off.
# 4 when either is violated, 3 when unreadable. The flags the run writes are STACK scope (the
# registry allows no narrower scope for them), so this guard is the protection of real games.
real_guard() {
  local live real
  live="$(real_game_ids)" || return 3
  if [[ -n "${live}" ]]; then
    echo "agent-group-ab: a real game is live (${live//$'\n'/ })" >&2
    return 4
  fi
  real="$("${FLAGS_SH}" variant agent policy.group_grant.real_games 2>/dev/null)" || {
    echo "agent-group-ab: cannot read policy.group_grant.real_games; refusing (fail closed)" >&2
    return 3
  }
  if [[ "${real}" != off ]]; then
    echo "agent-group-ab: policy.group_grant.real_games is '${real}', not off; refusing" >&2
    return 4
  fi
}

# snapshot_flags: one line per touched flag: flag, spec, writer, expiry epoch ("-" when empty).
# Refuses when a touched flag already carries a group-ab overlay (a run that was killed).
snapshot_flags() {
  local tmp="${OUT}/snapshot.tsv.tmp" flag view line
  : >"${tmp}"
  for flag in "${TOUCHED[@]}"; do
    view="$(flag_view "${flag}")" || { rm -f "${tmp}"; return 3; }
    line="$(tool overlay <<<"${view}")" || { rm -f "${tmp}"; return 3; }
    if [[ "${line}" == *$'\t'"${WRITER}"$'\t'* ]]; then
      echo "agent-group-ab: ${flag} already carries a ${WRITER} overlay (a killed run?); run: $0 restore --out <dir>" >&2
      rm -f "${tmp}"
      return 3
    fi
    printf '%s\t%s\n' "${flag}" "${line}" >>"${tmp}"
  done
  mv "${tmp}" "${OUT}/snapshot.tsv"
}

# restore_flags FILE: put each flag back, but only when its current stack overlay is still ours.
# An overlay another writer set meanwhile (or an admin write that cleared ours) is left alone.
restore_flags() {
  local file="$1" flag spec writer expires view cur cspec cwriter cexp rc=0 left
  while IFS=$'\t' read -r flag spec writer expires; do
    view="$(flag_view "${flag}")" || { echo "agent-group-ab: cannot read ${flag}; not restored" >&2; rc=3; continue; }
    cur="$(tool overlay <<<"${view}")" || { rc=3; continue; }
    IFS=$'\t' read -r cspec cwriter cexp <<<"${cur}"
    if [[ "${cwriter}" != "${WRITER}" ]]; then
      [[ "${cspec}" == none ]] || echo "agent-group-ab: ${flag} left as is: overlay by '${cwriter}' set during the run" >&2
      continue
    fi
    if [[ "${spec}" != none && "${expires}" != "-" ]]; then
      left="$(tool ttl-left "${expires}")"
      [[ "${left}" -gt 0 ]] || spec=none # the old overlay has expired meanwhile
    fi
    [[ "${writer}" == "-" ]] && writer="${WRITER}-restored"
    local ttl=()
    [[ "${spec}" != none && "${expires}" != "-" ]] && ttl=(--ttl "${left}")
    case "${spec}" in
      none) "${FLAGS_SH}" clear agent "${flag}" --writer "${WRITER}" || rc=3 ;;
      variant:*) "${FLAGS_SH}" set agent "${flag}" "${spec#variant:}" --writer "${writer}" "${ttl[@]}" || rc=3 ;;
      value:*) "${FLAGS_SH}" set-value agent "${flag}" "${spec#value:}" --writer "${writer}" "${ttl[@]}" || rc=3 ;;
      *) echo "agent-group-ab: bad snapshot line for ${flag}" >&2; rc=3 ;;
    esac
  done <"${file}"
  return "${rc}"
}

# recover DIR: flags back, leftover group-ab containers removed, the stack agent started again.
recover() {
  local dir="$1" rc=0 id
  if [[ -s "${dir}/snapshot.tsv" ]]; then
    if restore_flags "${dir}/snapshot.tsv"; then
      mv "${dir}/snapshot.tsv" "${dir}/snapshot.tsv.restored"
    else
      echo "agent-group-ab: flags not fully restored; fix by hand, then re-run: $0 restore --out ${dir}" >&2
      rc=3
    fi
  fi
  for id in $("${DOCKER}" ps -aq --filter "name=${CONTAINER_PREFIX}" 2>/dev/null || true); do
    "${DOCKER}" rm -f "${id}" >/dev/null 2>&1 || true
  done
  if [[ -e "${dir}/agent-was-running" ]]; then
    # shellcheck disable=SC2086 # COMPOSE may hold two words
    if ${COMPOSE} start "${AGENT_SERVICE}" >/dev/null 2>&1; then
      rm -f "${dir}/agent-was-running"
    else
      echo "agent-group-ab: could not start ${AGENT_SERVICE}; start it by hand" >&2
      rc=3
    fi
  fi
  return "${rc}"
}

cleanup() {
  local code=$?
  trap - EXIT INT TERM HUP
  if ((CLEANUP_ARMED)); then
    recover "${OUT}" || { ((code)) || code=3; }
  fi
  exit "${code}"
}

arm_profile() { case "$1" in A) echo v1 ;; B) echo neutral ;; esac; }

# run_game ARM SEED REP ARCHETYPE: one one-shot shadow game; appends the manifest line.
# Called as `run_game ... || exit $?`, which turns off `set -e` in here: every step is checked.
run_game() {
  local arm="$1" seed="$2" rep="$3" archetype="$4" profile name line="" deadline timeout rc=0 started ended tick=0
  profile="$(arm_profile "${arm}")"
  real_guard || return $?
  "${FLAGS_SH}" set agent decision.prompt_profile "${profile}" --writer "${WRITER}" >/dev/null || {
    echo "agent-group-ab: could not set decision.prompt_profile=${profile}; stopping" >&2
    return 3
  }
  name="${CONTAINER_PREFIX}$$-${arm}-${seed}-${rep}"
  started="$(date +%s%N)"
  # shellcheck disable=SC2086
  ${COMPOSE} run -d --no-deps ${ALIAS_ARGS} --name "${name}" -e AGENT_SHADOW_GAME=true -e SHADOW_GAME_MODE=JoustFFA \
    -e "SHADOW_GAME_ARCHETYPE=${archetype}" -e "SHADOW_GAME_SEED=${seed}" "${AGENT_SERVICE}" >/dev/null || return 3
  timeout="$("${HERE}/pattern-replay-check.sh" --show-timeout)" || timeout=420
  deadline=$((SECONDS + timeout))
  while ((SECONDS < deadline)); do
    line="$("${DOCKER}" logs "${name}" 2>/dev/null | grep -m1 '^PATTERN_REPLAY_RECORD ' || true)"
    [[ -n "${line}" ]] && break
    # every third second: a real game appearing aborts this game
    if ((++tick % 3 == 0)); then
      real_guard || rc=$?
      if ((rc)); then
        echo "agent-group-ab: aborting game ${arm} seed ${seed} rep ${rep}" >&2
        "${DOCKER}" rm -f "${name}" >/dev/null 2>&1 || true
        return "${rc}"
      fi
    fi
    sleep 1
  done
  ended="$(date +%s%N)"
  "${DOCKER}" rm -f "${name}" >/dev/null 2>&1 || true
  if [[ -z "${line}" ]]; then
    echo "agent-group-ab: game ${arm} seed ${seed} rep ${rep}: no PATTERN_REPLAY_RECORD within ${timeout}s" >&2
    return 3
  fi
  printf '%s\n' "${line#PATTERN_REPLAY_RECORD }" >"${OUT}/record.tmp" || return 3
  tool manifest-line --arm "${arm}" --profile "${profile}" --seed "${seed}" --rep "${rep}" \
    --archetype "${archetype}" --record-file "${OUT}/record.tmp" --started-ns "${started}" --ended-ns "${ended}" \
    >>"${OUT}/manifest.jsonl" || return 3
  echo "game ${arm} (${profile}) seed ${seed} rep ${rep}: done"
}

# verify_first_game: did the agent that decides in the game answer with this arm's profile?
# If not, every game would be excluded; stop before spending the budget.
verify_first_game() {
  local arm="$1" seed="$2" rep="$3" profile gid i
  ((VERIFY)) || return 0
  profile="$(arm_profile "${arm}")"
  gid="$(tail -n 1 "${OUT}/manifest.jsonl" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read())["game_id"])')" || return 3
  for ((i = 0; i < ${AB_VERIFY_TRIES:-8}; i++)); do
    real_guard || return $?
    if tool verify-game --game-id "${gid}" --profile "${profile}" "${VERIFY_ARGS[@]}"; then
      echo "verified: ${gid} has remote agent.decide spans with prompt_profile=${profile}"
      return 0
    fi
    sleep "${AB_VERIFY_WAIT:-3}"
  done
  echo "agent-group-ab: the first game (${gid}) has no remote agent.decide span with prompt_profile=${profile}." >&2
  echo "  The throwaway agent saw nothing (the collector sends spans to the 'agent' network alias), or no" >&2
  echo "  remote backend answered. Retry with --use-aliases (then the throwaway agent observes EVERY game," >&2
  echo "  real ones too: the real-game guard polls /games every 3 s), or check the backend. --no-verify skips this." >&2
  return 3
}

cmd_run() {
  local archetype="" seeds="1,2,3" reps=2 backend="jev"
  ALIAS_ARGS="" VERIFY=1 VERIFY_ARGS=(--jaeger-url "http://localhost/jaeger/api")
  while (($#)); do
    case "$1" in
      --archetype) archetype="${2:?}"; shift 2 ;;
      --seeds) seeds="${2:?}"; shift 2 ;;
      --reps) reps="${2:?}"; shift 2 ;;
      --backend) backend="${2:?}"; shift 2 ;;
      --out) OUT="${2:?}"; shift 2 ;;
      --use-aliases) ALIAS_ARGS="--use-aliases"; shift ;;
      --no-verify) VERIFY=0; shift ;;
      --jaeger-url) VERIFY_ARGS=(--jaeger-url "${2:?}"); shift 2 ;;
      --traces-file) VERIFY_ARGS=(--traces-file "${2:?}"); shift 2 ;;
      *) usage ;;
    esac
  done
  [[ -n "${archetype}" && -n "${OUT}" ]] || usage
  [[ "${reps}" =~ ^[1-9][0-9]*$ ]] || usage
  mkdir -p "${OUT}"
  if [[ -e "${OUT}/snapshot.tsv" ]]; then
    echo "agent-group-ab: ${OUT}/snapshot.tsv exists (a run was killed?); run: $0 restore --out ${OUT}" >&2
    exit 3
  fi
  if [[ -s "${OUT}/manifest.jsonl" ]]; then
    echo "agent-group-ab: ${OUT}/manifest.jsonl exists; use a new --out" >&2
    exit 3
  fi

  real_guard || exit $?
  # A kill switch (interventions_allowed=none, persisted or a stack overlay) would be overridden by our
  # stack overlay: refuse. An unreadable answer refuses too (fail closed).
  local adm eff
  adm="$(flag_view interventions_allowed | tool admin-variant)" || exit 3
  eff="$("${FLAGS_SH}" variant agent interventions_allowed)" || exit 3
  if [[ "${adm}" == none || "${eff}" == none ]]; then
    echo "agent-group-ab: interventions_allowed is 'none' (admin value '${adm}', effective '${eff}': the kill switch); the run would lift it. Refusing." >&2
    exit 4
  fi
  snapshot_flags || exit 3

  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP # a lost terminal: restore, then exit non-zero
  CLEANUP_ARMED=1
  if [[ -n "$(${COMPOSE} ps --status running -q "${AGENT_SERVICE}" 2>/dev/null || true)" ]]; then
    : >"${OUT}/agent-was-running"
    # shellcheck disable=SC2086
    ${COMPOSE} stop "${AGENT_SERVICE}" >/dev/null
  fi

  local flagset
  flagset() { "${FLAGS_SH}" set agent "$1" "$2" --writer "${WRITER}" >/dev/null; }
  flagset experiment_dynamic_enabled off
  flagset llm.eligible_game_kinds real_and_shadow
  flagset interventions_allowed shadow_group
  flagset policy.group_grant.enabled on
  flagset decision.backend "${backend}"

  : >"${OUT}/manifest.jsonl"
  local seed rep order arm first=1
  IFS=',' read -r -a seed_list <<<"${seeds}"
  for seed in "${seed_list[@]}"; do
    for ((rep = 1; rep <= reps; rep++)); do
      # A,B then B,A: the order alternates per repetition.
      if ((rep % 2 == 1)); then order="A B"; else order="B A"; fi
      for arm in ${order}; do
        run_game "${arm}" "${seed}" "${rep}" "${archetype}" || exit $?
        if ((first)); then
          first=0
          verify_first_game "${arm}" "${seed}" "${rep}" || exit $?
        fi
      done
    done
  done
  real_guard || exit $? # a real game that started during the last game: say so before the report
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
    tool report --manifest "${OUT}/manifest.jsonl" "${traces[@]}" "${json[@]}"
  else
    tool report --manifest "${OUT}/manifest.jsonl" --jaeger-url "${jaeger}" "${json[@]}"
  fi
}

cmd_restore() {
  [[ "${1:-}" == "--out" && -n "${2:-}" ]] || usage
  recover "$2"
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
