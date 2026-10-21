#!/usr/bin/env bash
# Stage helper for the "Two Standards Walk Into an Agent" talk (#743, #1301).
# Runbook: docs/talk/two-standards-runbook.md. Use case: UC-070 (BR-003, A7, A8, A9).
#
# The stage baseline (scripts/talk-baseline.json) is the flag state every act
# beat starts from; the shipped flag files keep their own, fail-closed defaults.
# All changes go through the flag service (scripts/flagservice.py, epic #1407).
#
# Usage:
#   ./scripts/talk-stage.sh reset
#       Clear every stage overlay in the flag service (whatever a beat left
#       behind), clear the persisted decision backend and model (an operator's
#       agent-backend.sh choice, #1682) so a restart cannot bring a remote
#       backend back, and set every baseline flag to its stage variant again.
#       Interventions are per game and need no reset (UC-053 BR-003).
#       Idempotent; run it between rehearsals and after any interrupted beat,
#       then ./scripts/talk-preflight.sh.
#   ./scripts/talk-stage.sh permissions <variant> [seconds]
#       The permissions beat (UC-070 A7): set interventions_allowed to <variant>
#       (e.g. none), hold it for [seconds] (default 12; 0 = until Enter), then
#       restore the baseline variant. The restore also runs when the beat is
#       interrupted (Ctrl-C, closed terminal, TERM). Only SIGKILL skips it: then
#       run `reset` (the pre-flight FAILs until you do).
#   ./scripts/talk-stage.sh fallback on|off
#       The screen-only fallback when Bluetooth fails at the venue (UC-070 A9,
#       #1316). `on` applies the `fallback` section of the baseline
#       (llm.eligible_game_kinds=real_and_shadow, experiments_enabled=on,
#       experiment_dynamic_enabled=on, experiment_tick_seconds=fast): the
#       experiment loop declares experiments
#       and runs archetype shadow games, and remote backends may decide on them;
#       the dashboard follows the newest live shadow game. `off` restores the
#       stage baseline of those flags (so does `reset`). The archetype selection
#       must include calm_long (a game that runs to the cap; `on` warns otherwise).
#   ./scripts/talk-stage.sh status
#       Show which baseline flags differ from the stage baseline, and whether
#       the fallback is on.
#
# Environment:
#   FLAG_SERVICE_URL    the flag service (default http://localhost/flags, the envoy route)
#   TALK_BASELINE_FILE  baseline (default scripts/talk-baseline.json)
#
# Every change is an in-memory stack overlay with writer=stage (epic #1407): the
# flag files never change, the stage never writes a persisted value, and the
# flag service pushes the change to flagd at once. A flag-service restart drops
# the stage overlays: run `reset` again. The decision backend and model are the
# exception on the operator side: ./scripts/agent-backend.sh persists them
# (C-018, #1682), so a stage overlay over a persisted remote backend is
# temporary and a restart or reboot brings the persisted backend back. `reset`
# therefore also deletes those persisted values (DELETE ...?persist=true): the
# flags return to the flag-file baseline (rules in the shipped files).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASELINE_FILE="${TALK_BASELINE_FILE:-${SCRIPT_DIR}/talk-baseline.json}"

usage() {
  cat >&2 <<EOF
usage: $0 reset
       $0 permissions <interventions_allowed-variant> [seconds]
       $0 fallback on|off
       $0 status
EOF
  exit 2
}

# flags <mode> [args...]: the one place that talks to the flag service.
flags() {
  PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 - "${BASELINE_FILE}" "$@" <<'PY'
import json
import sys

from flagservice import FlagService, FlagServiceError, FlagServiceUnreachable

baseline_path, mode, *args = sys.argv[1:]
with open(baseline_path) as fh:
    baseline = json.load(fh)

SET, WRITER = "agent", "stage"
# The flags agent-backend.sh writes persisted (#1682); reset clears them.
PERSISTED_BACKEND_KEYS = ("decision.backend", "decision.claude.model", "decision.jev.model", "decision.openai.model")
fs = FlagService.from_env()
fallback = baseline.get("fallback", {})


def set_variant(key, variant, effective, changes, problems):
    """Stage overlay (in memory, writer=stage, never a persisted write: re-review N4)."""
    current = effective.get(key, (None, None))[0]
    try:
        fs.set_variant(SET, key, variant, writer=WRITER)
    except FlagServiceError as err:
        problems.append(f"{key}: {err.reason}: {err.detail}")
        return
    if current != variant:
        changes.append(f"{key}: {current} -> {variant}")


def fallback_on(effective):
    return bool(fallback) and all(effective.get(k, (None,))[0] == v for k, v in fallback.items())


try:
    effective = fs.effective(SET) if mode != "baseline-of" else {}

    if mode == "status":
        drift = [
            f"{k} = {effective.get(k, (None,))[0]} (stage: {v})"
            for k, v in baseline["checked"].items()
            if effective.get(k, (None,))[0] != v
        ]
        print("\n".join(drift) if drift else "at the stage baseline")
        if fallback_on(effective):
            print("fallback: on (screen-only shadow games; ./scripts/talk-stage.sh fallback off)")
        sys.exit(0)

    if mode == "fallback":
        (state,) = args
        changes, problems = [], []
        for key, variant in fallback.items():
            stage_variant = baseline["checked"].get(key, baseline["reset_also"].get(key))
            set_variant(key, variant if state == "on" else stage_variant, effective, changes, problems)
        if problems:
            sys.exit("; ".join(problems))
        for line in changes:
            print(f"set    {line}")
        if state == "on":
            # A game must always be in progress (UC-070 BR-007): the selection needs
            # a long-running archetype.
            variant, names = fs.effective(SET).get("experiment.mock_archetypes", (None, ""))
            want = baseline.get("fallback_archetype", "calm_long")
            if want not in [n.strip() for n in str(names or "").split(",")]:
                print(f"WARN   experiment.mock_archetypes = {variant} ({names or 'nothing'}) has no "
                      f"{want}: shadow games may all end and leave the screen empty (reset restores 'all')")
            print("fallback on: shadow games start on the next experiment tick; the dashboard follows the newest one")
        else:
            print("fallback off: stage baseline restored for " + ", ".join(fallback))
        sys.exit(0)

    if mode == "set":
        key, variant = args
        changes, problems = [], []
        set_variant(key, variant, effective, changes, problems)
        if problems:
            sys.exit("; ".join(problems))
        print(f"{key} -> {variant}")
        sys.exit(0)

    if mode == "baseline-of":
        print(baseline["checked"][args[0]])
        sys.exit(0)

    if mode == "reset":
        # Drop whatever a beat left behind (every stage overlay) and the persisted
        # decision backend and model (#1682: a stage overlay over a persisted
        # remote backend would not survive a restart), then apply the stage
        # baseline again. The host's other persisted admin settings (e.g.
        # interventions_allowed from the menu) are not touched: a stage overlay
        # sits on top of them.
        fs.clear_writer(SET, WRITER)
        for view in fs.flag_set(SET)["flags"]:
            persisted = view.get("admin_value") or {}
            if view["flag"] in PERSISTED_BACKEND_KEYS and persisted:
                fs.clear(SET, view["flag"], persist=True)
                print(f"reset  {view['flag']}: persisted {persisted.get('variant')} cleared")
        effective = fs.effective(SET)
        changes, problems = [], []
        for section in ("checked", "reset_also"):
            for key, variant in baseline[section].items():
                set_variant(key, variant, effective, changes, problems)
        for line in changes:
            print(f"reset  {line}")
        for line in problems:
            print(f"skip   {line}")
        # Interventions are per game (UC-053 BR-003): nothing to reset; they are
        # dropped when their game ends.
        state = fs.state()
        print(f"interventions: {state.get('scope_values', 0)} game-scoped value(s) in "
              f"{state.get('live_games', 0)} live game(s); dropped when each game ends")
        print("stage baseline applied" if not problems else "stage baseline applied with skips (see above)")
        sys.exit(0)

    sys.exit(f"unknown mode {mode}")
except FlagServiceUnreachable as err:
    sys.exit(str(err))
PY
}

action="${1:-}"
case "${action}" in
  reset)
    [[ $# -eq 1 ]] || usage
    flags reset
    ;;
  status)
    [[ $# -eq 1 ]] || usage
    flags status
    ;;
  fallback)
    [[ $# -eq 2 && ( "$2" == "on" || "$2" == "off" ) ]] || usage
    flags fallback "$2"
    ;;
  permissions)
    [[ $# -ge 2 && $# -le 3 ]] || usage
    variant="$2"
    hold="${3:-12}"
    [[ "${hold}" =~ ^[0-9]+$ ]] || usage
    restore_to="$(flags baseline-of interventions_allowed)"
    restored=0
    sleeper=""
    restore() {
      [[ "${restored}" -eq 1 ]] && return 0
      restored=1
      if [[ -n "${sleeper}" ]]; then kill "${sleeper}" 2>/dev/null || true; fi
      flags set interventions_allowed "${restore_to}"
    }
    # Restore on every way out but SIGKILL (UC-070 A7): normal end, Ctrl-C,
    # closed terminal, TERM.
    trap restore EXIT
    trap 'exit 130' INT
    trap 'exit 129' HUP
    trap 'exit 143' TERM
    flags set interventions_allowed "${variant}"
    if [[ "${hold}" -eq 0 ]]; then
      read -r -p "press Enter to restore interventions_allowed=${restore_to} " _ || true
    else
      echo "holding ${hold}s, then restoring interventions_allowed=${restore_to} (Ctrl-C restores now)"
      # sleep in the background + wait, so a signal is handled at once.
      sleep "${hold}" &
      sleeper=$!
      wait "${sleeper}" || true
    fi
    ;;
  *)
    usage
    ;;
esac
