#!/usr/bin/env bash
# Stage helper for the "Two Standards Walk Into an Agent" talk (#743, #1301).
# Runbook: docs/talk/two-standards-runbook.md. Use case: UC-070 (BR-003, A7, A8, A9).
#
# The stage baseline (scripts/talk-baseline.json) is the flag state every act
# beat starts from; the shipped flag files keep their own, fail-closed defaults.
#
# Usage:
#   ./scripts/talk-stage.sh reset
#       Set every baseline flag in the agent flag file to its stage variant
#       (whatever a beat left behind) and every flag in the interventions.json
#       next to it to its neutral variant (none / default / off, targeting
#       removed). Idempotent; run it between rehearsals and after any
#       interrupted beat, then ./scripts/talk-preflight.sh.
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
#       experiment_dynamic_enabled=on): the experiment loop declares experiments
#       and runs archetype shadow games, and remote backends may decide on them;
#       the dashboard follows the newest live shadow game. `off` restores the
#       stage baseline of those flags (so does `reset`). The archetype selection
#       must include calm_long (a game that runs to the cap; `on` warns otherwise).
#   ./scripts/talk-stage.sh status
#       Show which baseline flags differ from the stage baseline, and whether
#       the fallback is on.
#
# Environment:
#   AGENT_FLAG_FILE     flag file to edit (default services/flagd/ci/agent.json;
#                       on the Pi hardware stack: services/flagd/agent.json)
#   TALK_BASELINE_FILE  baseline (default scripts/talk-baseline.json)
#
# Writes are in place (same inode), like agent-backend.sh, so single-file bind
# mounts keep working; flagd reloads the file in about a second.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FLAG_FILE="${AGENT_FLAG_FILE:-${REPO_ROOT}/services/flagd/ci/agent.json}"
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

# flags <mode> [args...]: the one place that reads and writes the flag files.
flags() {
  python3 - "${FLAG_FILE}" "${BASELINE_FILE}" "$@" <<'PY'
import json
import os
import sys

path, baseline_path, mode, *args = sys.argv[1:]
with open(baseline_path) as fh:
    baseline = json.load(fh)


def load(p):
    with open(p) as fh:
        return json.load(fh)


def write_in_place(p, data):
    # In place on purpose (same inode): see agent-backend.sh write_in_place().
    with open(p, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")


def set_variant(flags, key, variant, changes, problems):
    flag = flags.get(key)
    if flag is None:
        problems.append(f"{key}: not in the flag file")
    elif variant not in flag.get("variants", {}):
        problems.append(f"{key}: no variant {variant!r} (have {list(flag.get('variants', {}))})")
    elif flag.get("defaultVariant") != variant:
        changes.append(f"{key}: {flag.get('defaultVariant')} -> {variant}")
        flag["defaultVariant"] = variant


data = load(path)
flags = data.get("flags", {})

fallback = baseline.get("fallback", {})


def fallback_on():
    return bool(fallback) and all(flags.get(k, {}).get("defaultVariant") == v for k, v in fallback.items())


if mode == "status":
    drift = [
        f"{k} = {flags.get(k, {}).get('defaultVariant')} (stage: {v})"
        for k, v in baseline["checked"].items()
        if flags.get(k, {}).get("defaultVariant") != v
    ]
    print("\n".join(drift) if drift else "at the stage baseline")
    if fallback_on():
        print("fallback: on (screen-only shadow games; ./scripts/talk-stage.sh fallback off)")
    sys.exit(0)

if mode == "fallback":
    (state,) = args
    changes, problems = [], []
    for key, variant in fallback.items():
        stage_variant = baseline["checked"].get(key, baseline["reset_also"].get(key))
        target = variant if state == "on" else stage_variant
        set_variant(flags, key, target, changes, problems)
    if problems:
        sys.exit("; ".join(problems))
    if changes:
        write_in_place(path, data)
    for line in changes:
        print(f"set    {line}")
    if state == "on":
        # A game must always be in progress (UC-070 BR-007): the selection needs
        # a long-running archetype.
        sel = flags.get("experiment.mock_archetypes", {})
        names = str(sel.get("variants", {}).get(sel.get("defaultVariant")) or "")
        want = baseline.get("fallback_archetype", "calm_long")
        if want not in [n.strip() for n in names.split(",")]:
            print(f"WARN   experiment.mock_archetypes = {sel.get('defaultVariant')} ({names or 'nothing'}) has no "
                  f"{want}: shadow games may all end and leave the screen empty (reset restores 'all')")
        print("fallback on: shadow games start on the next experiment tick; the dashboard follows the newest one")
    else:
        print("fallback off: stage baseline restored for " + ", ".join(fallback))
    sys.exit(0)

if mode == "set":
    key, variant = args
    changes, problems = [], []
    set_variant(flags, key, variant, changes, problems)
    if problems:
        sys.exit("; ".join(problems))
    if changes:
        write_in_place(path, data)
    print(f"{key} -> {variant}  ({path})")
    sys.exit(0)

if mode == "baseline-of":
    print(baseline["checked"][args[0]])
    sys.exit(0)

if mode == "reset":
    changes, problems = [], []
    for section in ("checked", "reset_also"):
        for key, variant in baseline[section].items():
            set_variant(flags, key, variant, changes, problems)
    if changes:
        write_in_place(path, data)
    for line in changes:
        print(f"reset  {line}")
    for line in problems:
        print(f"skip   {line}")

    # interventions.json next to the agent flag file: back to neutral variants.
    ivpath = os.path.join(os.path.dirname(path), "interventions.json")
    if os.path.exists(ivpath):
        iv = load(ivpath)
        ivchanges = []
        for key, flag in iv.get("flags", {}).items():
            variants = flag.get("variants", {})
            neutral = next((v for v in ("none", "default", "off") if v in variants), None)
            if neutral is not None and flag.get("defaultVariant") != neutral:
                ivchanges.append(f"{key}: {flag.get('defaultVariant')} -> {neutral}")
                flag["defaultVariant"] = neutral
            # Only agent-written targeting goes; the shipped empty "targeting": {}
            # placeholders stay as they are.
            if flag.get("targeting"):
                del flag["targeting"]
                ivchanges.append(f"{key}: targeting removed")
        if ivchanges:
            write_in_place(ivpath, iv)
        for line in ivchanges:
            print(f"reset  interventions.json {line}")
    else:
        print(f"skip   {ivpath} not found")
    print("stage baseline applied" if not problems else "stage baseline applied with skips (see above)")
    sys.exit(0)

sys.exit(f"unknown mode {mode}")
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
