#!/usr/bin/env bash
# Self-contained tests for scripts/ci/detach.sh (#2036): start, PENDING, attach,
# finished result, REFUSED on a different command, a killed waiter, and the
# pr-merger use (a fake safe-merge.sh whose own exit 3 is not PENDING).
#
# Runs as part of `make lint`, or directly: ./scripts/tests/test-detach.sh
set -euo pipefail
# Hermetic: a fail-loud fake docker first on PATH, no COMPOSE / DOCKER override.
unset COMPOSE DOCKER
# Hermetic: no detach state from a caller (e.g. this test run inside a gate-run job).
unset GATE_RUN_WORKER GATE_RUN_WAIT GATE_RUN_DRY_RUN DETACH_WAIT
FAKE_DOCKER_DIR="$(mktemp -d)"
printf '#!/bin/sh\necho "test reached docker: $*" >&2\nexit 99\n' >"${FAKE_DOCKER_DIR}/docker"
chmod +x "${FAKE_DOCKER_DIR}/docker"
export PATH="${FAKE_DOCKER_DIR}:${PATH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETACH="${SCRIPT_DIR}/../ci/detach.sh"
WORK="$(realpath "$(mktemp -d)")"
trap 'rm -rf "${WORK}" "${FAKE_DOCKER_DIR}"' EXIT

# A fake safe-merge.sh: counts its starts, blocks on the fifo $FAKE_GATE (when set)
# until the test writes to it (#2073: no timing), prints progress on stderr and ONE result line on stdout, exits $FAKE_RC (3 = its own head mismatch).
FAKE="${WORK}/safe-merge.sh"
cat >"$FAKE" <<'EOF'
#!/usr/bin/env bash
n=$(( $(cat "$FAKE_COUNTER" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$FAKE_COUNTER"
echo "waiting for CI on #$1" >&2
if [[ -n "${FAKE_GATE:-}" ]]; then read -r _ <"$FAKE_GATE"; fi
if [[ "${FAKE_RC:-0}" == 3 ]]; then echo "HEAD MISMATCH: #$1"; exit 3; fi
echo "MERGED #$1 at $2"
EOF
chmod +x "$FAKE"
export FAKE_COUNTER="${WORK}/counter"
starts() { cat "$FAKE_COUNTER" 2>/dev/null || echo 0; }

# gate_open <name>: a fifo the test controls. fd 7 is read-write, so opening never blocks
# and a byte written to it releases exactly one job blocked in `read` (#2073).
gate_open() { mkfifo "${WORK}/gate-$1"; exec 7<>"${WORK}/gate-$1"; }
gate_release() { echo go >&7; exec 7>&-; }
# wait_for_file <path>: poll for an event (not a duration); fail only after 30 s.
wait_for_file() {
  local _
  for _ in $(seq 300); do [[ -f "$1" ]] && return 0; sleep 0.1; done
  fail "never appeared: $1"
}

pass=0
fail() { echo "FAIL: $*" >&2; exit 1; }
# run_d <expected exit> <expected output regex> args...
run_d() {
  local want_rc="$1" want_re="$2" out rc
  shift 2
  set +e
  # timeout: a detach regression that loops must fail the test, not hang `make lint`.
  out="$(timeout 60 "${DETACH}" "$@" 2>&1)"
  rc=$?
  set -e
  [[ "$rc" == "$want_rc" ]] || fail "exit $rc (want $want_rc) for: $*"$'\n'"$out"
  [[ "$out" =~ $want_re ]] || fail "output does not match /$want_re/ for: $*"$'\n'"$out"
  pass=$((pass + 1))
}

# --- argument validation
run_d 2 'REFUSED: --dir must be absolute' --dir rel -- "$FAKE" 1 abc
run_d 2 "REFUSED: --dir contains '..'" --dir "${WORK}/a/../b" -- "$FAKE" 1 abc
run_d 2 'REFUSED: wait must be 0-580' --dir "${WORK}/w" --wait 600 -- "$FAKE" 1 abc
run_d 2 'REFUSED: no command' --dir "${WORK}/w"
[[ ! -e "${WORK}/w" ]] || fail "a refused call created the job dir"

# --- PENDING, then the result; the pr-merger loop
J="${WORK}/merge-1"
gate_open 1
export FAKE_GATE="${WORK}/gate-1"
run_d 3 'VERDICT: PENDING \([0-9]+s\); rerun the same command$' --dir "$J" --wait 1 -- "$FAKE" 1 abc
wait_for_file "$J/pid"
[[ -f "$J/pid" && -f "$J/lock" && -f "$J/cmd" && ! -f "$J/rc" ]] || fail "running job dir: want pid/lock/cmd, no rc"
run_d 2 'REFUSED: .*different command' --dir "$J" --wait 1 -- "$FAKE" 2 abc
gate_release
calls=0 rc=3
while ((rc == 3 && calls < 22)); do
  calls=$((calls + 1))
  set +e; out="$("$DETACH" --dir "$J" --wait 1 -- "$FAKE" 1 abc 2>&1)"; rc=$?; set -e
  [[ "$rc" == 3 && "$(tail -n 1 <<<"$out")" != "VERDICT: PENDING"* ]] && break
done
[[ "$rc" == 0 && "$(tail -n 1 <<<"$out")" == "MERGED #1 at abc" ]] || fail "loop result: rc=$rc $out"
pass=$((pass + 1))
# Finished: the same call prints the same result and starts nothing.
run_d 0 'MERGED #1 at abc$' --dir "$J" -- "$FAKE" 1 abc
[[ "$(starts)" == 1 ]] || fail "identical calls started $(starts) jobs"
# A finished job dir is single use: a different command stays REFUSED.
run_d 2 'REFUSED: .*different command' --dir "$J" -- "$FAKE" 1 def

# --- the wrapped command's own exit 3 is a result, not PENDING
rm -f "$FAKE_COUNTER"
FAKE_GATE="" FAKE_RC=3 run_d 3 'HEAD MISMATCH: #2$' --dir "${WORK}/merge-2" -- "$FAKE" 2 abc
out="$("$DETACH" --dir "${WORK}/merge-2" -- "$FAKE" 2 abc 2>&1 || true)"
[[ "$out" != *"VERDICT: PENDING"* ]] || fail "a real exit 3 read as PENDING"
pass=$((pass + 1))

# --- concurrent identical first calls: one job
rm -f "$FAKE_COUNTER"
export FAKE_GATE=""
J3="${WORK}/merge-3"
for _ in 1 2 3; do timeout 60 "$DETACH" --dir "$J3" --wait 0 -- "$FAKE" 3 abc >/dev/null 2>&1 & done
wait || true
run_d 0 'MERGED #3 at abc$' --dir "$J3" --wait 10 -- "$FAKE" 3 abc
[[ "$(starts)" == 1 ]] || fail "concurrent first calls started $(starts) jobs"

# --- a killed waiter (its whole process group) leaves the job alive
rm -f "$FAKE_COUNTER"
J4="${WORK}/merge-4"
gate_open 4
export FAKE_GATE="${WORK}/gate-4"
setsid timeout 60 "$DETACH" --dir "$J4" --wait 30 -- "$FAKE" 4 abc >/dev/null 2>&1 &
waiter=$!
wait_for_file "$J4/pid"
kill -KILL -- "-${waiter}" 2>/dev/null || kill -KILL "$waiter"
wait "$waiter" 2>/dev/null || true
kill -0 "$(cat "$J4/pid")" 2>/dev/null || fail "job died with its waiter"
gate_release
run_d 0 'MERGED #4 at abc$' --dir "$J4" --wait 10 -- "$FAKE" 4 abc
[[ "$(starts)" == 1 ]] || fail "killed waiter: job restarted"

# --- a job killed before it wrote rc: ERROR: killed, never restarted
rm -f "$FAKE_COUNTER"
J5="${WORK}/merge-5"
gate_open 5
FAKE_GATE="${WORK}/gate-5" run_d 3 'VERDICT: PENDING' --dir "$J5" --wait 0 -- "$FAKE" 5 abc
wait_for_file "$J5/pid"
pkill -KILL -s "$(cat "$J5/pid")" || true
for _ in $(seq 300); do flock -n "$J5/lock" true && break; sleep 0.1; done
flock -n "$J5/lock" true || fail "killed job still holds its lock"
exec 7>&-
run_d 1 'VERDICT: ERROR: killed' --dir "$J5" --wait 1 -- "$FAKE" 5 abc
[[ "$(starts)" == 1 ]] || fail "a killed job was restarted"

# --- the job can trap Ctrl-C: SIGINT is not ignored (a `&` start would ignore it)
INTCMD="${WORK}/int.sh"
cat >"$INTCMD" <<'EOS'
#!/usr/bin/env bash
trap 'echo "got INT"; exit 0' INT
kill -INT $$
sleep 1
echo "INT ignored"
exit 1
EOS
chmod +x "$INTCMD"
run_d 0 'got INT' --dir "${WORK}/int" --wait 10 -- "$INTCMD"

echo "test-detach: ${pass} checks passed"
