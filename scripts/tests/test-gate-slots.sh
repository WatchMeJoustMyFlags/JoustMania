#!/usr/bin/env bash
# Tests the host-wide gate slots of scripts/ci/gate-run.sh (#2210): with
# JOUSTMANIA_GATE_SLOTS=1 a second job waits ("waiting for a gate slot") until the
# first one ends; a killed worker frees its slot.
#
# Runs as part of `make lint`, or directly: ./scripts/tests/test-gate-slots.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"
unset COMPOSE DOCKER GATE_RUN_WORKER GATE_RUN_WAIT GATE_RUN_DRY_RUN DETACH_WAIT
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATE_RUN="${SCRIPT_DIR}/../ci/gate-run.sh"
WORK="$(realpath "$(mktemp -d)")"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export JOUSTMANIA_GATE_SLOTS=1 JOUSTMANIA_GATE_SLOT_DIR="${WORK}/slots"
MAIN="${WORK}/main" WT="${WORK}/main/.worktrees/wt" LOGS="${WORK}/logs"
git init -q -b main "$MAIN"
git -C "$MAIN" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
git -C "$MAIN" worktree add -q "$WT" -b wt
mkdir -p "$WT/scripts/tests"
GATE="${WORK}/gate"
mkfifo "$GATE"
export GATE
# job "hold" blocks on the fifo; job "quick" prints and ends.
printf 'read -r _ <"$GATE"\necho held\n' >"$WT/scripts/tests/test-hold.sh"
printf 'echo quick\n' >"$WT/scripts/tests/test-quick.sh"

cleanup() {
  exec 5<>"$GATE" 2>/dev/null && printf 'go\ngo\ngo\n' >&5 # release any blocked job
  pkill -KILL -f "$WT/scripts/tests/" 2>/dev/null || true
  rm -rf "${WORK}"
}
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }
slot_held() { ! flock -n "$WORK/slots/slot.1" true 2>/dev/null; } # false when the file is missing or free
slot_held_probe() { [[ -e "$WORK/slots/slot.1" ]] && slot_held; }
run() { GATE_RUN_WAIT="$1" "$GATE_RUN" --worktree "$WT" --logs "$LOGS" --name "$2" -- "scripts/tests/test-$2.sh"; }

run 0 hold >"$WORK/hold.out" || true
# wait until the first worker holds the slot
wait_slot() { for _ in $(seq 100); do slot_held_probe && return 0; sleep 0.2; done; return 1; }
wait_slot || fail "first job does not hold slot.1"

run 0 quick >"$WORK/quick.out" || true
sleep 2
grep -q 'VERDICT: PENDING' "$WORK/quick.out" || fail "second job should be PENDING: $(cat "$WORK/quick.out")"
J2="$(find "$LOGS" -name quick.job -type d)"
for _ in $(seq 25); do grep -q 'waiting for a gate slot' "$J2/out" 2>/dev/null && break; sleep 0.2; done
grep -q 'waiting for a gate slot' "$J2/out" || fail "second job did not announce waiting"
[[ ! -f "$J2/rc" ]] || fail "second job ran while the slot was held"

echo go >"$GATE" # release the first job
out="$(run 60 hold)" || fail "first job failed: $out"
grep -q 'VERDICT: PASS' <<<"$out" || fail "first job: $out"
out="$(run 60 quick)" || fail "second job failed: $out"
grep -q 'VERDICT: PASS' <<<"$out" || fail "second job: $out"
grep -q 'waiting for a gate slot' <<<"$out" || fail "waiting line missing from the verdict output"

# a killed worker frees the slot
printf 'read -r _ <"$GATE"\n' >"$WT/scripts/tests/test-doomed.sh"
run 0 doomed >/dev/null || true
wait_slot || fail "doomed job does not hold slot.1"
pkill -KILL -f "gate-run.sh --_worker.*--name doomed" || fail "no doomed worker found"
pkill -KILL -f "scripts/tests/test-doomed.sh" || true
for _ in $(seq 25); do slot_held_probe || break; sleep 0.2; done
[[ -e "$WORK/slots/slot.1" ]] && flock -n "$WORK/slots/slot.1" true || fail "slot not released after kill"
# memory cap: a run above the cap gets the cap verdict (systemd scopes only)
if systemd-run --user --scope -q -p MemoryMax=64M -p MemorySwapMax=0 -- true >/dev/null 2>&1; then
  cat >"$WT/scripts/tests/test-hog.sh" <<'HOG'
python3 -c "b = bytearray(600*1024*1024); b[::4096] = b'x' * len(b[::4096]); print(len(b))"
HOG
  out="$(JOUSTMANIA_GATE_MEM=256M run 60 hog)" && fail "hog should not pass: $out"
  grep -q 'killed: memory cap' <<<"$out" || fail "no cap verdict: $out"
  grep -q '^VERDICT: FAIL' <<<"$out" || fail "hog verdict: $out"
else
  echo "SKIP: no systemd-run --user"
fi
echo "PASS: gate slots"
