#!/usr/bin/env bash
# Detach-and-await: run one long command as a fully detached job and wait for it
# in bounded foreground calls (#2036).
#
# Why: an agent that starts work with the Bash tool's `run_in_background` and then
# "waits for the notification" goes idle and hands back early. A foreground Bash
# call is capped at 10 minutes. This script lets an agent stay in the foreground:
# each call waits at most --wait seconds, and the job itself survives the call.
#
# Usage:
#   scripts/ci/detach.sh --dir <abs job dir> [--wait <seconds>] -- <command argv...>
#
#   --dir   absolute job directory (created; its parent is created too). It holds
#           cmd (the argv, one %q word per line), started (epoch), pid, out (the
#           command's stdout+stderr), rc (its exit code, written last) and lock
#           (a flock the job holds while it runs).
#   --wait  seconds to wait for the result in this call, 0-580 (default
#           DETACH_WAIT or 540: a foreground Bash call is killed at 600 s)
#
# Semantics:
#   - First call: start the command with setsid + nohup, stdin/stdout/stderr
#     redirected, in its own session (a killed caller, or its process group, does
#     not take the job down). Then wait like any later call.
#   - Same argv again: attach to the running job, or print the finished result.
#     It never starts a second job; a job dir is single use. Use a new --dir for
#     a new run.
#   - Different argv for the same --dir: REFUSED (exit 2).
#   - Wait: when rc appears, print out and exit with the command's exit code.
#     Otherwise print `VERDICT: PENDING (<elapsed>s); rerun the same command`
#     and exit 3. A job that ended without rc (killed): `VERDICT: ERROR: killed
#     ...`, exit 1; it is never restarted.
#   Exit 3 is also a real exit code of some wrapped commands (safe-merge.sh: head
#   mismatch): a caller loops only while the exit is 3 AND the last line starts
#   with `VERDICT: PENDING`.
#
# The command runs as given (no shell, no eval); the job's children do not
# inherit the lock. Linux only (setsid, flock, GNU mv -T).
#
# Test: scripts/tests/test-detach.sh (runs in `make lint`).
set -uo pipefail

refuse() {
  echo "REFUSED: $*"
  exit 2
}

# ---------------------------------------------------------------- job side
# Internal: `detach.sh --_job <dir> -- <cmd...>`, started by the first call with
# the lock already held on fd 9. Runs the command, writes rc atomically, exits
# (which releases the lock).
if [[ "${1:-}" == --_job ]]; then
  JOB="$2"
  shift 3
  echo "$$" >"$JOB/pid"
  "$@" >"$JOB/out" 2>&1 </dev/null 9>&-
  rc=$?
  echo "$rc" >"$JOB/rc.tmp" && mv -f -- "$JOB/rc.tmp" "$JOB/rc"
  exit 0
fi

# ---------------------------------------------------------------- arguments
DIR="" WAIT="${DETACH_WAIT:-540}"
while (($#)); do
  case "$1" in
    --dir) (($# >= 2)) || refuse "--dir needs a value"; DIR="$2"; shift 2 ;;
    --wait) (($# >= 2)) || refuse "--wait needs a value"; WAIT="$2"; shift 2 ;;
    --) shift; break ;;
    *) refuse "unknown argument: $1" ;;
  esac
done
(($#)) || refuse "no command after --"
[[ "$DIR" == /* ]] || refuse "--dir must be absolute: $DIR"
[[ "$DIR" != *..* ]] || refuse "--dir contains '..': $DIR"
DIR="${DIR%/}"
[[ "$WAIT" =~ ^[0-9]{1,3}$ ]] && ((WAIT <= 580)) || refuse "wait must be 0-580 seconds: $WAIT"
for tool in setsid nohup flock; do
  command -v "$tool" >/dev/null || refuse "$tool not found (Linux util-linux needed)"
done

want_cmd() { printf '%q\n' "$@"; }
SELF="$(realpath -e -- "${BASH_SOURCE[0]}")"

# ---------------------------------------------------------------- start or attach
mkdir -p -- "$(dirname -- "$DIR")" || refuse "cannot create parent of $DIR"
[[ -d "$DIR" ]] && rmdir -- "$DIR" 2>/dev/null  # an empty dir is not a job yet
if [[ ! -e "$DIR" ]]; then
  # Build the job dir aside, take the lock, then rename it into place: the rename
  # is atomic, so a concurrent identical call either sees no dir or a complete,
  # locked one. Losing the race just means attaching to the winner's job.
  tmp="$(mktemp -d -- "$DIR.new.XXXXXX")" || refuse "cannot create $DIR"
  want_cmd "$@" >"$tmp/cmd"
  date +%s >"$tmp/started"
  exec 9>"$tmp/lock"
  flock -n 9 || refuse "cannot lock $tmp/lock"
  if mv -T -- "$tmp" "$DIR" 2>/dev/null; then
    setsid nohup "$SELF" --_job "$DIR" -- "$@" </dev/null >/dev/null 2>&1 &
    echo "detach: started job $DIR (pid $!)"
  else
    rm -rf -- "$tmp"
  fi
  exec 9>&-
fi
[[ -f "$DIR/cmd" ]] || refuse "$DIR exists but is not a job dir"
[[ "$(cat -- "$DIR/cmd")" == "$(want_cmd "$@")" ]] ||
  refuse "$DIR holds a different command; use a new --dir (running or finished: $(tr '\n' ' ' <"$DIR/cmd"))"

# ---------------------------------------------------------------- wait
job_alive() { ! flock -n "$DIR/lock" true 2>/dev/null; }
finish() {
  cat -- "$DIR/out" 2>/dev/null
  exit "$(cat -- "$DIR/rc")"
}

deadline=$((SECONDS + WAIT))
while :; do
  [[ -f "$DIR/rc" ]] && finish
  if ! job_alive; then
    [[ -f "$DIR/rc" ]] && finish
    tail -n 5 -- "$DIR/out" 2>/dev/null
    echo "VERDICT: ERROR: killed (the job in $DIR ended without an exit code); it is not restarted"
    exit 1
  fi
  ((SECONDS < deadline)) || break
  # Poll fast at first (short jobs), then every 2 s.
  if ((SECONDS < 5)); then sleep 0.2; else sleep 2; fi
done
started="$(cat -- "$DIR/started" 2>/dev/null || date +%s)"
echo "VERDICT: PENDING ($(($(date +%s) - started))s); rerun the same command"
exit 3
