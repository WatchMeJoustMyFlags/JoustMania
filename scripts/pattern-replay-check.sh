#!/usr/bin/env bash
# Replay check for pattern-driven shadow games (UC-046 steps 9-10, A11; FR-110).
#
# Runs one archetype twice with the agent's one-shot shadow runner on the running
# stack and compares the two replay records:
#   - per-slot checkpoint hashes on their common prefix (NFR-040)
#   - elimination order and winner slot (NFR-041)
#   - the sequence of decisions as intervention:slot (NFR-042)
#   - parameter and flag digests (UC-046 BR-009, BR-011)
# Only games with replay_exact=true on both sides are compared.
#
# Usage:
#   scripts/pattern-replay-check.sh <archetype> [seed]
#   scripts/pattern-replay-check.sh --against <record.json>
#   scripts/pattern-replay-check.sh --compare <record-a.json> <record-b.json>
#   scripts/pattern-replay-check.sh --show-timeout
#
# --against replays one recorded game (UC-046 step 9): the record is a saved
# PATTERN_REPLAY_RECORD JSON or one `pattern_game_ended` line of an experiment's
# journal (events.jsonl). It runs the recorded archetype and seed once and compares.
# An experiment arm whose resolved flags differ from the base flags (the arm under
# test) has a different flag digest, so it reports "not comparable"; the control
# arm and one-shot games are comparable.
#
# Each run starts a throwaway agent container (`docker compose run`) with
# AGENT_SHADOW_GAME=true, SHADOW_GAME_ARCHETYPE and SHADOW_GAME_SEED, waits for the
# PATTERN_REPLAY_RECORD line it prints, and removes the container. Needs the stack
# up (`docker compose up -d`). For a decision comparison, keep interventions off or
# stop the stack's own agent first so only the throwaway agent observes the game.
#
# The image is whatever `docker compose` resolves for the service: the stack's IMAGE_TAG
# or, when unset, the release pin in .env. Export IMAGE_TAG=<the running stack's tag> to
# replay on the same image as the stack.
#
# Environment: COMPOSE (default "docker compose"), PATTERN_REPLAY_TIMEOUT seconds
# per run (default: the effective safety cap `experiment.max_game_seconds` read from
# the flag service plus 120 s start-up margin, so 420 s for the default 300 s cap and
# 720 s / 1920 s for the long / soak caps; UC-046 A16), PATTERN_REPLAY_SERVICE
# (default "agent"), FLAGS_SH (flag helper, default scripts/flags.sh; FLAG_SERVICE_URL
# as there).
#
# Exit codes: 0 identical, 1 the replay differs, 2 not comparable (non-exact or a
# digest differs), 3 usage or run error.
set -euo pipefail

# UC-046 A16, BR-013: the wait follows the pinned safety cap, never a fixed 420 s.
# cap_seconds: the effective experiment.max_game_seconds; the agent falls back to 300
# for a value outside 30..3600, and so do we when the flag service cannot be read.
cap_seconds() {
  local flags_sh="${FLAGS_SH:-$(dirname "${BASH_SOURCE[0]}")/flags.sh}" view
  view="$("${flags_sh}" get agent experiment.max_game_seconds 2>/dev/null)" || view=""
  python3 -c '
import json, sys
cap = 300
try:
    view = json.loads(sys.argv[1])
    overlay = view.get("stack_overlay") or {}
    value = overlay.get("value")
    if not isinstance(value, (int, float)):
        value = json.loads(str(view["variant_values"][view["effective_default"]]))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 30 <= value <= 3600:
        cap = int(value)
except Exception:
    pass
print(cap)
' "${view}"
}

# replay_timeout: seconds to wait per run; the env override wins, else cap + 120 s start-up.
replay_timeout() {
  if [[ -n "${PATTERN_REPLAY_TIMEOUT:-}" ]]; then
    echo "${PATTERN_REPLAY_TIMEOUT}"
  else
    echo $(($(cap_seconds) + 120))
  fi
}

# normalize <file>: print a replay record from a record or a journal event.
normalize() {
  python3 - "$1" <<'PY'
import json, sys

text = open(sys.argv[1]).read()
try:
    doc = json.loads(text)
except ValueError:
    # A journal (JSON lines): use the last pattern_game_ended event.
    events = [json.loads(line) for line in text.splitlines() if line.strip()]
    ended = [e for e in events if e.get("kind") == "pattern_game_ended"]
    if not ended:
        sys.exit("no pattern_game_ended event in " + sys.argv[1])
    doc = ended[-1]
if doc.get("kind") == "pattern_game_ended":
    p = doc.get("pattern") or {}
    doc = {
        "game_id": doc.get("game_id", ""),
        "archetype": p.get("archetype", ""),
        "params_digest": p.get("params_digest", ""),
        "rng_seed": str(p.get("rng_seed", "")),
        "flag_digest": p.get("flag_digest", ""),
        "outcome": p.get("outcome", ""),
        "elimination_order": p.get("elimination_order") or [],
        "winner_slot": p.get("winner_slot", -1),
        "signal_checkpoints": p.get("signal_checkpoints") or [],
        "replay_exact": bool(p.get("replay_exact")),
        "non_exact_reasons": p.get("non_exact_reasons") or [],
        "decisions": p.get("decisions") or [],
    }
print(json.dumps(doc))
PY
}

compare() {
  python3 - "$1" "$2" <<'PY'
import json, sys

a, b = (json.load(open(p)) for p in sys.argv[1:3])

def bail(code, msg):
    print(msg)
    sys.exit(code)

for side, rec in (("first", a), ("second", b)):
    if not rec.get("replay_exact"):
        bail(2, f"not comparable: {side} game is non-exact: {','.join(rec.get('non_exact_reasons') or [])}")
for key in ("archetype", "params_digest", "flag_digest"):
    if a.get(key) != b.get(key):
        bail(2, f"not comparable: {key} differs ({a.get(key)} vs {b.get(key)})")
if a.get("flag_digest") in ("", "unavailable"):
    bail(2, "not comparable: the flag digest is unavailable")

ca, cb = a.get("signal_checkpoints") or [], b.get("signal_checkpoints") or []
if len(ca) != len(cb):
    bail(1, f"differs: slot count {len(ca)} vs {len(cb)}")
for slot, (xa, xb) in enumerate(zip(ca, cb)):
    for second, (ha, hb) in enumerate(zip(xa or [], xb or []), start=1):
        if ha != hb:
            bail(1, f"differs: slot {slot} checkpoint at second {second} ({ha} vs {hb})")
for key in ("elimination_order", "winner_slot", "outcome"):
    if a.get(key) != b.get(key):
        bail(1, f"differs: {key} {a.get(key)} vs {b.get(key)}")
da, db = a.get("decisions") or [], b.get("decisions") or []
for i, (x, y) in enumerate(zip(da, db)):
    if x != y:
        bail(1, f"differs: decision {i} {x} vs {y}")
if len(da) != len(db):
    bail(1, f"differs: decision count {len(da)} vs {len(db)}")
common = min((min(len(x or []), len(y or [])) for x, y in zip(ca, cb)), default=0)
print(f"identical: archetype {a.get('archetype')} seed {a.get('rng_seed')}: "
      f"checkpoints equal over >= {common} s per slot, elimination order {a.get('elimination_order')}, "
      f"winner slot {a.get('winner_slot')}, {len(da)} decisions")
PY
}

run_once() {
  local archetype="$1" seed="$2" out="$3" tag="$4"
  local compose="${COMPOSE:-docker compose}"
  local service="${PATTERN_REPLAY_SERVICE:-agent}"
  local timeout
  timeout="$(replay_timeout)"
  local name="pattern-replay-$$-${tag}"
  local args=(-e AGENT_SHADOW_GAME=true -e SHADOW_GAME_MODE=JoustFFA -e "SHADOW_GAME_ARCHETYPE=${archetype}")
  [[ -n "${seed}" ]] && args+=(-e "SHADOW_GAME_SEED=${seed}")
  # shellcheck disable=SC2086 # COMPOSE may hold two words ("docker compose")
  ${compose} run -d --no-deps --name "${name}" "${args[@]}" "${service}" >/dev/null
  local deadline=$((SECONDS + timeout)) line=""
  while ((SECONDS < deadline)); do
    line="$(docker logs "${name}" 2>/dev/null | grep -m1 '^PATTERN_REPLAY_RECORD ' || true)"
    [[ -n "${line}" ]] && break
    sleep 2
  done
  docker rm -f "${name}" >/dev/null 2>&1 || true
  if [[ -z "${line}" ]]; then
    echo "run ${tag}: no PATTERN_REPLAY_RECORD within ${timeout}s" >&2
    return 3
  fi
  printf '%s\n' "${line#PATTERN_REPLAY_RECORD }" >"${out}"
}

main() {
  if [[ "${1:-}" == "--show-timeout" ]]; then
    replay_timeout
    return
  fi
  if [[ "${1:-}" == "--compare" ]]; then
    [[ $# -eq 3 ]] || { echo "usage: $0 --compare <record-a.json> <record-b.json>" >&2; exit 3; }
    WORK_DIR="$(mktemp -d)"
    trap 'rm -rf "${WORK_DIR}"' EXIT
    normalize "$2" >"${WORK_DIR}/a.json" && normalize "$3" >"${WORK_DIR}/b.json" || exit 3
    compare "${WORK_DIR}/a.json" "${WORK_DIR}/b.json"
    return
  fi
  if [[ "${1:-}" == "--against" ]]; then
    [[ $# -eq 2 ]] || { echo "usage: $0 --against <record.json>" >&2; exit 3; }
    WORK_DIR="$(mktemp -d)"
    trap 'rm -rf "${WORK_DIR}"' EXIT
    normalize "$2" >"${WORK_DIR}/recorded.json" || exit 3
    local archetype seed
    archetype="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("archetype") or "")' "${WORK_DIR}/recorded.json")"
    seed="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("rng_seed") or "")' "${WORK_DIR}/recorded.json")"
    [[ -n "${archetype}" ]] || { echo "record has no archetype" >&2; exit 3; }
    run_once "${archetype}" "${seed}" "${WORK_DIR}/replay.json" replay || exit 3
    compare "${WORK_DIR}/recorded.json" "${WORK_DIR}/replay.json"
    return
  fi
  [[ $# -ge 1 && $# -le 2 && -n "$1" ]] || { echo "usage: $0 <archetype> [seed] | --against <record> | --compare <a> <b>" >&2; exit 3; }
  local archetype="$1" seed="${2:-}"
  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "${WORK_DIR}"' EXIT
  run_once "${archetype}" "${seed}" "${WORK_DIR}/a.json" a || exit 3
  run_once "${archetype}" "${seed}" "${WORK_DIR}/b.json" b || exit 3
  compare "${WORK_DIR}/a.json" "${WORK_DIR}/b.json"
}

main "$@"
