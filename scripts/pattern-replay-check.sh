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
#   scripts/pattern-replay-check.sh --compare <record-a.json> <record-b.json>
#
# Each run starts a throwaway agent container (`docker compose run`) with
# AGENT_SHADOW_GAME=true, SHADOW_GAME_ARCHETYPE and SHADOW_GAME_SEED, waits for the
# PATTERN_REPLAY_RECORD line it prints, and removes the container. Needs the stack
# up (`docker compose up -d`). For a decision comparison, keep interventions off or
# stop the stack's own agent first so only the throwaway agent observes the game.
#
# Environment: COMPOSE (default "docker compose"), PATTERN_REPLAY_TIMEOUT seconds
# per run (default 420: the 300 s safety cap plus start-up), PATTERN_REPLAY_SERVICE
# (default "agent").
#
# Exit codes: 0 identical, 1 the replay differs, 2 not comparable (non-exact or a
# digest differs), 3 usage or run error.
set -euo pipefail

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
  local timeout="${PATTERN_REPLAY_TIMEOUT:-420}"
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
  if [[ "${1:-}" == "--compare" ]]; then
    [[ $# -eq 3 ]] || { echo "usage: $0 --compare <record-a.json> <record-b.json>" >&2; exit 3; }
    compare "$2" "$3"
    return
  fi
  [[ $# -ge 1 && $# -le 2 && -n "$1" ]] || { echo "usage: $0 <archetype> [seed]" >&2; exit 3; }
  local archetype="$1" seed="${2:-}"
  WORK_DIR="$(mktemp -d)"
  trap 'rm -rf "${WORK_DIR}"' EXIT
  run_once "${archetype}" "${seed}" "${WORK_DIR}/a.json" a || exit 3
  run_once "${archetype}" "${seed}" "${WORK_DIR}/b.json" b || exit 3
  compare "${WORK_DIR}/a.json" "${WORK_DIR}/b.json"
}

main "$@"
