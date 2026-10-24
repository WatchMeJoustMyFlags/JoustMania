#!/usr/bin/env bash
# Validated gate/test runner for the gate-runner agent (#1565).
#
# The gate-runner agent may run ONLY this script. The allow-list lives here, in
# code, not in the agent's prompt: the command is passed as separate argv words
# after `--`, checked word by word against fixed forms, and executed directly
# (no eval, no `bash -c`, no shell parsing of caller input).
#
# Usage:
#   scripts/ci/gate-run.sh --worktree <abs path> --logs <abs dir> \
#       [--repeat N] [--expect-fail] [--name <slug>] [--dir <rel dir>] -- <command argv...>
#
#   --worktree  absolute path of a git worktree under <main checkout>/.worktrees/
#   --logs      absolute log directory OUTSIDE the worktree (created if missing)
#   --repeat    run the command N times in a row, 1-20 (default 1)
#   --expect-fail  mutation check: the caller weakened the code and expects a test FAILURE
#   --name      log slug, [a-z0-9-] (default "run"); logs are
#               <logs>/<worktree-basename>-<head8>/<name>-<i>.log, so runs of other
#               worktrees or heads sharing one --logs dir never overwrite each other
#               (#2036); the verdict header and the `Logs:` line show the exact paths
#   --dir       working directory relative to the worktree (required for go / cargo:
#               the directory holding go.mod / Cargo.toml)
#
# Allowed commands (anything else is REFUSED):
#   make <target> [VAR=value ...]
#       targets: pr-gates, lint, format-check, check, specs-check, spec-refs-check, test, test-dev, test-pulled,
#                test-contract, test-contract-flagd,
#                test-service[-*], test-integration
#       VAR in: TEST SERVICE BASE PR_LABELS IMAGE_TAG PYTEST_EXTRA
#                SERVICE_TESTS (test-service only: paths under tests/service, no "..")
#   uv run --package <workspace member> [--extra <name>]... pytest [args]
#   go test|vet [args]          (--dir with go.mod)
#   cargo test|clippy [args]    (--dir with Cargo.toml)
#   scripts/tests/test-<name>.sh [args]   (or: bash scripts/tests/test-<name>.sh)
# The dashboard (vitest / tsc) runs only through `make pr-gates SERVICE=dashboard`.
#
# Threat model: guards against agent and prompt mistakes (what the caller adds to a
# command), not a sandbox for untrusted PR code. Every allowed form runs the
# worktree's own code (Makefile recipes, pytest conftest/plugins, go test binaries,
# cargo build.rs, scripts/tests). Run it only on PRs you would run locally.
# Accepted risk: pytest -p/-o/--rootdir/--import-mode and go -ldflags/-gcflags pass
# validation, but can only reach worktree or venv code because absolute paths and
# '..' are refused; the threat model covers that, so there is no per-option allow-list.
#
# Detach / await (#2036): the run is a detached job (scripts/ci/detach.sh) in
# <logs>/<worktree-basename>-<head8>/<name>.job/. One call waits at most GATE_RUN_WAIT
# seconds (default 540, 0-580); when the run is not done yet it prints
# `VERDICT: PENDING (<n>s); rerun the same command` and exits 3. Rerunning the same
# command attaches to the job (never a second one). A finished job whose command or
# worktree state differs, and a killed job, are replaced by a fresh one. Never run
# this script with the Bash tool's run_in_background: loop on exit 3 in the
# foreground instead.
#
# Exit: 0 verdict as expected (PASS, or MUTATION CAUGHT (assertion) with --expect-fail),
#       1 any other verdict (also ERROR: killed), 2 REFUSED, 3 PENDING.
#
# Test: scripts/tests/test-gate-run.sh (runs in `make lint`).
set -uo pipefail

refuse() {
  echo "REFUSED: $*"
  exit 2
}

# ---------------------------------------------------------------- arguments
WT="" LOGS="" REPEAT=1 EXPECT_FAIL=0 NAME="run" RUN_DIR_REL="" WORKER=0
# Internal first argument: the detached job re-runs this script as the worker. An
# argv flag, not an env var, so nothing leaks into the run's environment (#2036).
if [[ "${1:-}" == --_worker ]]; then
  WORKER=1
  shift
fi
while (($#)); do
  case "$1" in
    --worktree) (($# >= 2)) || refuse "--worktree needs a value"; WT="$2"; shift 2 ;;
    --logs) (($# >= 2)) || refuse "--logs needs a value"; LOGS="$2"; shift 2 ;;
    --repeat) (($# >= 2)) || refuse "--repeat needs a value"; REPEAT="$2"; shift 2 ;;
    --expect-fail) EXPECT_FAIL=1; shift ;;
    --name) (($# >= 2)) || refuse "--name needs a value"; NAME="$2"; shift 2 ;;
    --dir) (($# >= 2)) || refuse "--dir needs a value"; RUN_DIR_REL="$2"; shift 2 ;;
    --) shift; break ;;
    *) refuse "unknown argument: $1" ;;
  esac
done
CMD=("$@")
((${#CMD[@]})) || refuse "no command after --"

[[ "$REPEAT" =~ ^[0-9]{1,2}$ ]] && ((REPEAT >= 1 && REPEAT <= 20)) || refuse "repeat must be 1-20: $REPEAT"
[[ "$NAME" =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]] || refuse "bad name: $NAME"

# ---------------------------------------------------------------- worktree
[[ "$WT" == /* ]] || refuse "worktree must be absolute: $WT"
[[ "$WT" != *..* ]] || refuse "worktree contains '..': $WT"
WT="${WT%/}"
[[ -d "$WT" ]] || refuse "worktree not a directory: $WT"
WT_REAL="$(realpath -e -- "$WT" 2>/dev/null)" || refuse "worktree not resolvable: $WT"
top="$(git -C "$WT_REAL" rev-parse --show-toplevel 2>/dev/null)" || refuse "not a git worktree: $WT"
[[ "$(realpath -e -- "$top")" == "$WT_REAL" ]] || refuse "not a worktree root: $WT"
common="$(git -C "$WT_REAL" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" ||
  refuse "no git common dir: $WT"
MAIN="$(dirname -- "$(realpath -e -- "$common")")"
case "$WT_REAL" in
  "$MAIN"/.worktrees/?*) ;;
  *) refuse "worktree not under $MAIN/.worktrees/: $WT" ;;
esac
[[ "$(git -C "$WT_REAL" rev-parse --git-dir 2>/dev/null)" != "$common" ]] ||
  refuse "main checkout, not a linked worktree: $WT"

# ---------------------------------------------------------------- logs dir
[[ "$LOGS" == /* ]] || refuse "logs dir must be absolute: $LOGS"
[[ "$LOGS" != *..* ]] || refuse "logs dir contains '..': $LOGS"
LOGS="${LOGS%/}"
case "$(realpath -m -- "$LOGS")/" in
  "$WT_REAL"/*) refuse "logs dir inside the worktree: $LOGS" ;;
esac
# Never write logs into the main checkout or any worktree.
case "$(realpath -m -- "$LOGS")/" in
  "$MAIN"/* | */.worktrees/*) refuse "logs dir under the main checkout or a .worktrees/ path: $LOGS" ;;
esac
mkdir -p -- "$LOGS" || refuse "cannot create logs dir: $LOGS"
LOGS="$(realpath -e -- "$LOGS")"
case "$LOGS/" in
  "$WT_REAL"/*) refuse "logs dir inside the worktree: $LOGS" ;;
esac

# ---------------------------------------------------------------- run dir
RUN_DIR="$WT_REAL"
if [[ -n "$RUN_DIR_REL" ]]; then
  [[ "$RUN_DIR_REL" =~ ^[A-Za-z0-9_][A-Za-z0-9_./-]*$ ]] || refuse "bad dir: $RUN_DIR_REL"
  [[ "$RUN_DIR_REL" != *..* ]] || refuse "dir contains '..': $RUN_DIR_REL"
  RUN_DIR="$(realpath -e -- "$WT_REAL/$RUN_DIR_REL" 2>/dev/null)" || refuse "dir does not exist: $RUN_DIR_REL"
  case "$RUN_DIR/" in "$WT_REAL"/*) ;; *) refuse "dir escapes the worktree: $RUN_DIR_REL" ;; esac
  [[ -d "$RUN_DIR" ]] || refuse "dir is not a directory: $RUN_DIR_REL"
fi

# ---------------------------------------------------------------- command
# Every word: a conservative character set (no shell metacharacters, `$`, quotes
# other than a single quote, newlines; [] allowed for pytest ids), no '..', no absolute paths.
# A '..' path component (Go's ./... pattern is fine).
DOTDOT_RE='(^|[/=: ])\.\.(/|$| )'
WORD_RE="^[][A-Za-z0-9^_.,/:=@+' -]*$"
for w in "${CMD[@]}"; do
  [[ "$w" =~ $WORD_RE ]] || refuse "forbidden character in: $w"
  [[ ! "$w" =~ $DOTDOT_RE ]] || refuse "'..' in: $w"
  [[ "$w" != /* && "$w" != *=/* ]] || refuse "absolute path in: $w"
  case "$w" in
    -exec | -exec=* | --exec | --exec=* | -toolexec | -toolexec=* | --toolexec | --toolexec=* | \
      -vettool | -vettool=* | -overlay | -overlay=* | -modfile | -modfile=* | \
      -f | --file | --file=* | --makefile | --makefile=* | -C | --directory* | \
      --config | --config=* | -Z | -Z?* | --manifest-path | --manifest-path=* | \
      -c | --eval* | -e | --environment-overrides | *SHELL=* | MAKEFLAGS=* | MAKEFILES=*)
      refuse "forbidden option: $w" ;;
  esac
done

MAKE_TARGET_RE='^(pr-gates|lint|format-check|check|specs-check|spec-refs-check|test|test-dev|test-pulled|test-contract|test-contract-flagd|test-service(-[a-z0-9]+)*|test-integration)$'
MAKE_VAR_RE='^(TEST|SERVICE|BASE|PR_LABELS|IMAGE_TAG|PYTEST_EXTRA|SERVICE_TESTS)=(.*)$'
MAKE_VALUE_RE="^[A-Za-z0-9_.,/:=' -]*$"

validate_make() {
  [[ -z "$RUN_DIR_REL" ]] || refuse "make runs at the worktree root (no --dir)"
  (($# >= 2)) || refuse "make needs a target"
  [[ "$2" =~ $MAKE_TARGET_RE ]] || refuse "make target not allowed: $2"
  local w name value
  for w in "${@:3}"; do
    [[ "$w" =~ $MAKE_VAR_RE ]] || refuse "make argument not allowed: $w"
    name="${BASH_REMATCH[1]}" value="${BASH_REMATCH[2]}"
    [[ "$value" =~ $MAKE_VALUE_RE ]] || refuse "bad value for $name: $value"
    case "$name" in
      SERVICE) [[ "$value" =~ ^[a-z_,:-]+$ ]] || refuse "bad SERVICE: $value" ;;
      SERVICE_TESTS)
        local tok toks
        read -r -a toks <<<"$value"
        ((${#toks[@]} > 0)) || refuse "empty SERVICE_TESTS"
        for tok in "${toks[@]}"; do
          [[ "$tok" =~ ^tests/service(/[A-Za-z0-9_.-]+)*$ && "$tok" != *..* ]] \
            || refuse "bad SERVICE_TESTS token: $tok"
        done ;;
      BASE) [[ "$value" =~ ^[A-Za-z0-9._/-]+$ && "$value" != -* ]] || refuse "bad BASE: $value" ;;
    esac
  done
  # `make test*` depends on clean-test-venv, which runs `sudo rm -rf .venv-test`
  # when that venv is not writable. Never let it reach sudo.
  if [[ -d "$WT_REAL/.venv-test" && ! -w "$WT_REAL/.venv-test" ]]; then
    refuse ".venv-test is not writable (clean-test-venv would sudo rm it); fix ownership by hand"
  fi
}

validate_uv() {
  [[ "${2:-}" == run ]] || refuse "only 'uv run' is allowed"
  local i=3 member=""
  while ((i <= $#)); do
    local w="${!i}"
    case "$w" in
      --package)
        ((i + 1 <= $#)) || refuse "--package needs a value"
        i=$((i + 1)); member="${!i}" ;;
      --extra)
        ((i + 1 <= $#)) || refuse "--extra needs a value"
        i=$((i + 1)); [[ "${!i}" =~ ^[a-z0-9-]+$ ]] || refuse "bad extra: ${!i}" ;;
      pytest) break ;;
      *) refuse "uv run option not allowed: $w" ;;
    esac
    i=$((i + 1))
  done
  ((i <= $#)) || refuse "uv run must run pytest"
  [[ -n "$member" ]] || refuse "uv run needs --package <workspace member>"
  [[ "$member" =~ ^joustmania-[a-z-]+$ ]] || refuse "bad package: $member"
  grep -qsxF "name = \"$member\"" "$WT_REAL"/lib/pyproject.toml "$WT_REAL"/services/*/pyproject.toml \
    "$WT_REAL"/tests/*/pyproject.toml || refuse "not a workspace member: $member"
}

validate_go() {
  [[ "${2:-}" == test || "${2:-}" == vet ]] || refuse "only 'go test' and 'go vet' are allowed"
  [[ -n "$RUN_DIR_REL" && -f "$RUN_DIR/go.mod" ]] || refuse "go needs --dir <service dir with go.mod>"
}

validate_cargo() {
  [[ "${2:-}" == test || "${2:-}" == clippy ]] || refuse "only 'cargo test' and 'cargo clippy' are allowed"
  [[ -n "$RUN_DIR_REL" && -f "$RUN_DIR/Cargo.toml" ]] || refuse "cargo needs --dir <crate dir with Cargo.toml>"
}

# scripts/tests/test-<name>.sh, a regular file in the worktree; always run with bash.
validate_test_script() {
  [[ -z "$RUN_DIR_REL" ]] || refuse "scripts/tests run at the worktree root (no --dir)"
  [[ "$1" =~ ^scripts/tests/test-[A-Za-z0-9_-]+\.sh$ ]] || refuse "not a scripts/tests/test-*.sh: $1"
  [[ -f "$WT_REAL/$1" && ! -L "$WT_REAL/$1" ]] || refuse "no such test script: $1"
  local resolved
  resolved="$(realpath -e -- "$WT_REAL/$1" 2>/dev/null)" || refuse "no such test script: $1"
  case "$resolved" in
    "$WT_REAL"/scripts/tests/?*) ;;
    *) refuse "test script resolves outside scripts/tests/ (symlinked directory?): $1" ;;
  esac
}

case "${CMD[0]}" in
  make) validate_make "${CMD[@]}" ;;
  uv) validate_uv "${CMD[@]}" ;;
  go) validate_go "${CMD[@]}" ;;
  cargo) validate_cargo "${CMD[@]}" ;;
  bash)
    ((${#CMD[@]} >= 2)) || refuse "bash needs a scripts/tests/test-*.sh"
    validate_test_script "${CMD[1]}"
    CMD=("${CMD[@]:1}") ;;
  scripts/tests/*) validate_test_script "${CMD[0]}" ;;
  *) refuse "command not allowed: ${CMD[0]}" ;;
esac
[[ "${CMD[0]}" == scripts/tests/* ]] && CMD=(bash "${CMD[@]}")

if [[ "${GATE_RUN_DRY_RUN:-}" == 1 ]]; then  # used by scripts/tests/test-gate-run.sh
  echo "OK: dir=${RUN_DIR#"$WT_REAL"} cmd=${CMD[*]}"
  exit 0
fi

# ---------------------------------------------------------------- run
# Do not inherit settings that change what the tools execute.
unset MAKEFLAGS MAKEFILES GNUMAKEFLAGS GOFLAGS PYTEST_ADDOPTS BASH_ENV ENV RUSTC_WRAPPER CARGO_BUILD_RUSTC_WRAPPER

HEAD_SHA="$(git -C "$WT_REAL" rev-parse HEAD)"
LOGS_BASE="$LOGS"
# Namespace the logs per worktree and head: a shared --logs dir stays safe.
LOGS="$LOGS/$(basename -- "$WT_REAL")-${HEAD_SHA:0:8}"
mkdir -p -- "$LOGS" || refuse "cannot create logs dir: $LOGS"
JOB="$LOGS/$NAME.job"

# Worktree state the job ran on: HEAD, uncommitted changes, untracked files.
tree_fingerprint() {
  {
    git -C "$WT_REAL" rev-parse HEAD
    git --no-optional-locks -C "$WT_REAL" diff HEAD --binary
    git -C "$WT_REAL" ls-files -o --exclude-standard
    git -C "$WT_REAL" ls-files -o --exclude-standard -z | (cd "$WT_REAL" && xargs -0r git hash-object --)
  } 2>/dev/null | sha256sum | cut -c1-16
}

# ---------------------------------------------------------------- detach / await (#2036)
# The caller's call only waits; the run itself is a detached job (scripts/ci/detach.sh)
# in $JOB, started by re-running this script as the worker with the same validated
# arguments. Each call waits up to GATE_RUN_WAIT s (default 540, max 580) and prints
# the verdict, or `VERDICT: PENDING (<n>s); rerun the same command` with exit 3.
# - Identical call (same worktree, head, name, repeat, expect-fail, dir, command):
#   attaches to the running job, or prints its verdict when it has finished and the
#   worktree state is still the one it ran on. Never a second job.
# - Running job with a different command under the same name: REFUSED.
# - Finished job, but a different command or a changed worktree (a mutation check
#   with the same command after another edit): a fresh job replaces it.
# - A killed job (no rc, its lock free) is replaced the same way.
# The check, the removal and the start run under a flock on $JOB.lock, so two
# concurrent callers never remove each other's fresh job.
if ((!WORKER)); then
  SELF="$(realpath -e -- "${BASH_SOURCE[0]}")"
  DETACH="$(dirname -- "$SELF")/detach.sh"
  worker=("$SELF" --_worker --worktree "$WT_REAL" --logs "$LOGS_BASE" --name "$NAME" --repeat "$REPEAT")
  ((EXPECT_FAIL)) && worker+=(--expect-fail)
  [[ -n "$RUN_DIR_REL" ]] && worker+=(--dir "$RUN_DIR_REL")
  worker+=(-- "${CMD[@]}")
  WAIT="${GATE_RUN_WAIT:-540}"
  [[ "$WAIT" =~ ^[0-9]{1,3}$ ]] && ((WAIT <= 580)) || refuse "wait must be 0-580 seconds: $WAIT"
  echo "gate-run $WT_REAL head ${HEAD_SHA:0:8} job $NAME"
  echo "cmd: ${RUN_DIR_REL:+(in $RUN_DIR_REL) }${CMD[*]}"
  exec 8>"$JOB.lock"
  flock 8 || refuse "cannot lock $JOB.lock"
  if [[ -f "$JOB/rc" ]]; then
    if [[ "$(cat -- "$JOB/cmd" 2>/dev/null)" != "$(printf '%q\n' "${worker[@]}")" ||
      "$(cat -- "$JOB/tree" 2>/dev/null)" != "$(tree_fingerprint)" ]]; then
      rm -rf -- "$JOB"
    fi
  elif [[ -f "$JOB/lock" ]] && flock -n "$JOB/lock" true 2>/dev/null && [[ ! -f "$JOB/rc" ]]; then
    # killed: ended without rc and nobody holds its lock. The rc re-check matters: a job
    # that finished between the rc test above and the probe is finished, not killed (#2073).
    rm -rf -- "$JOB"
  fi
  # Start (or attach) without waiting, still under the lock; then wait without it.
  start_out="$("$DETACH" --dir "$JOB" --wait 0 -- "${worker[@]}" 2>&1 8>&-)"
  start_rc=$?
  exec 8>&-
  if ((start_rc == 2)); then
    echo "$start_out"
    exit 2
  fi
  grep -m1 '^detach: started' <<<"$start_out"
  exec "$DETACH" --dir "$JOB" --wait "$WAIT" -- "${worker[@]}"
fi
# Worker (inside the detached job): nothing of the caller's detach state reaches the
# run (a nested gate-run, e.g. test-gate-run.sh under `make lint`, detaches itself).
unset GATE_RUN_WORKER GATE_RUN_WAIT GATE_RUN_DRY_RUN DETACH_WAIT
# Record the state this run sees, then run.
{ tree_fingerprint >"$JOB/tree"; } 2>/dev/null

TEST_FAIL_RE='FAILED|AssertionError|\bassert\b|--- FAIL|panicked'
# scripts/tests/*.sh assert by printing their own `FAIL...` line and exiting non-zero
# (`FAIL: x`, `FAIL - x`, `FAIL name: x`, `FAILED: x`).
[[ "${CMD[0]}" == bash ]] && TEST_FAIL_RE+='|^[[:space:]]*FAIL(ED)?([: ]|$)'
NON_TEST_RE='ERROR collecting|errors? during collection|SyntaxError|IndentationError|ModuleNotFoundError|ImportError while|command not found|No such file or directory|Cannot connect to the Docker daemon|could not compile|build failed|no Go files|cannot find package|setup failed|No rule to make target'

failed=0 non_test=0
declare -a logs=() failed_logs=()
for ((i = 1; i <= REPEAT; i++)); do
  log="$LOGS/$NAME-$i.log"
  logs+=("$log")
  start=$SECONDS
  (cd "$RUN_DIR" && exec "${CMD[@]}") >"$log" 2>&1 </dev/null
  rc=$?
  echo "run $i/$REPEAT: exit $rc $((SECONDS - start))s"
  if ((rc != 0)); then
    failed=$((failed + 1))
    failed_logs+=("$log")
    if grep -qE "$NON_TEST_RE" "$log" || ! grep -qE "$TEST_FAIL_RE" "$log"; then
      non_test=$((non_test + 1))
    fi
  fi
done

# pr-gates prints its own per-gate table.
for log in "${logs[@]}"; do
  sed -n '/^== pr-gates summary/,/^$/p' "$log"
done

if ((failed == 0)); then verdict="PASS"
elif ((failed == REPEAT)); then verdict="FAIL"
else verdict="FLAKY $failed/$REPEAT"
fi
expected=0
[[ "$verdict" == PASS ]] && expected=1
if ((EXPECT_FAIL)); then
  expected=0
  if ((failed < REPEAT)); then
    verdict+="  MUTATION NOT CAUGHT ($((REPEAT - failed))/$REPEAT passed)"
  elif ((non_test == 0)); then
    verdict+="  MUTATION CAUGHT (assertion)"; expected=1
  else
    verdict+="  CAUGHT? (non-test error in $non_test/$REPEAT: collection, syntax, tool or docker; read the log)"
  fi
fi
echo "VERDICT: $verdict"
((REPEAT == 1 && EXPECT_FAIL)) &&
  echo "note: N=1 expect-fail = one run failed; it does not show the failure is deterministic (use --repeat 3+ for timing-sensitive tests)."
# FLAKY k/N counts FAILED repeats (k of N failed).

if ((${#failed_logs[@]})); then
  echo "Failing lines (<=20):"
  {
    for log in "${failed_logs[@]}"; do
      if grep -qE 'FAIL|Error|error:|assert|panic|Traceback|E  ' "$log"; then
        grep -nE 'FAIL|Error|error:|assert|panic|Traceback|E  ' "$log" | grep -vE ' PASS ' | sed "s|^|$log:|"
      else
        tail -n 5 "$log" | sed "s|^|$log:tail: |"
      fi
    done
  } | head -n 20 | sed 's/^/  /'
fi
echo "Logs: ${logs[*]}"
((expected)) && exit 0 || exit 1
