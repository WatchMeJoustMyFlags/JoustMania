#!/usr/bin/env bash
# Self-contained tests for scripts/ci/gate-run.sh (#1565).
#
# Builds a throwaway "main checkout" git repo with a linked worktree under
# .worktrees/, then checks that every injection vector from the PR #1570 review
# is REFUSED, that the real gate forms are accepted (GATE_RUN_DRY_RUN=1: validate
# only), and that the verdicts (PASS / FAIL / FLAKY k/N / MUTATION CAUGHT /
# CAUGHT?) come out right on real runs of stub test scripts.
#
# Runs as part of `make lint`, or directly: ./scripts/tests/test-gate-run.sh
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
GATE_RUN="${SCRIPT_DIR}/../ci/gate-run.sh"
WORK="$(realpath "$(mktemp -d)")"
trap 'rm -rf "${WORK}" "${FAKE_DOCKER_DIR}"' EXIT

# Own gate slots: this test may itself run inside a gate-run job holding a host slot.
export JOUSTMANIA_GATE_SLOT_DIR="${WORK}/slots"
MAIN="${WORK}/main"
WT="${MAIN}/.worktrees/wt"
LOGS="${WORK}/logs"
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1

git init -q -b main "${MAIN}"
git -C "${MAIN}" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
git -C "${MAIN}" worktree add -q "${WT}" -b wt
mkdir -p "${WT}/services/agent" "${WT}/services/rust-hid" "${WT}/services/menu" "${WT}/scripts/tests" "${WORK}/outside"
echo "module x" >"${WT}/services/agent/go.mod"
echo "[package]" >"${WT}/services/rust-hid/Cargo.toml"
printf '[project]\nname = "joustmania-menu"\n' >"${WT}/services/menu/pyproject.toml"
cat >"${WT}/scripts/tests/test-pass.sh" <<'EOF'
echo "all good"
EOF
cat >"${WT}/scripts/tests/test-assert.sh" <<'EOF'
echo "FAILED tests/test_x.py::test_y - AssertionError: assert 1 == 2"
exit 1
EOF
cat >"${WT}/scripts/tests/test-collect.sh" <<'EOF'
echo "ERROR collecting tests/test_x.py"
echo "E   SyntaxError: invalid syntax"
exit 2
EOF
# Fails on odd-numbered calls (counter file outside the worktree).
cat >"${WT}/scripts/tests/test-flaky.sh" <<'EOF'
n=$(( $(cat "$FLAKY_COUNTER" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$FLAKY_COUNTER"
if (( n % 2 )); then echo "--- FAIL: TestX"; exit 1; fi
echo ok
EOF
ln -s test-pass.sh "${WT}/scripts/tests/test-link.sh"
# Counts its starts (counter outside the worktree), then blocks on the fifo $SLOW_GATE
# when set (the test releases it, #2073), else runs for 3 s.
cat >"${WT}/scripts/tests/test-slow.sh" <<'EOF'
n=$(( $(cat "$SLOW_COUNTER" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$SLOW_COUNTER"
if [[ -n "${SLOW_GATE:-}" ]]; then exec 3<>"$SLOW_GATE"; read -r -t 60 _ <&3; else sleep 3; fi
echo "slow done"
EOF
# Fails when the caller's detach state reached the run (#2036 review: a nested
# gate-run, as in test-gate-run.sh under `make lint`, must detach itself).
cat >"${WT}/scripts/tests/test-env.sh" <<'EOF'
if [[ -n "${GATE_RUN_WORKER:-}${GATE_RUN_WAIT:-}${DETACH_WAIT:-}" ]]; then echo "FAIL: detach env leaked"; exit 1; fi
echo "env clean"
EOF
# A shell test's own assertion line (scripts/tests style), #2036.
cat >"${WT}/scripts/tests/test-shfail.sh" <<'EOF'
echo "FAIL: expected exit 3, got 0" >&2
exit 1
EOF

pass=0
fail() { echo "FAIL: $*" >&2; exit 1; }

# run_gr <expected exit> <expected output regex> args...
run_gr() {
  local want_rc="$1" want_re="$2" out rc
  shift 2
  set +e
  # timeout: a detach regression that loops must fail the test, not hang `make lint`.
  out="$(timeout 60 "${GATE_RUN}" "$@" 2>&1)"
  rc=$?
  set -e
  [[ "$rc" == "$want_rc" ]] || fail "exit $rc (want $want_rc) for: $*"$'\n'"$out"
  [[ "$out" =~ $want_re ]] || fail "output does not match /$want_re/ for: $*"$'\n'"$out"
  pass=$((pass + 1))
}
refused() { GATE_RUN_DRY_RUN=1 run_gr 2 '^REFUSED' --worktree "$WT" --logs "$LOGS" "$@"; }
accepted() { GATE_RUN_DRY_RUN=1 run_gr 0 '^OK: ' --worktree "$WT" --logs "$LOGS" "$@"; }

# --- worktree validation
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: worktree must be absolute' --worktree .worktrees/wt --logs "$LOGS" -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 "REFUSED: worktree contains '..'" --worktree "${WT}/../wt" --logs "$LOGS" -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: worktree not under' --worktree "$MAIN" --logs "$LOGS" -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: not a worktree root' --worktree "${WT}/services" --logs "$LOGS" -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: not a git worktree' --worktree "${WORK}/outside" --logs "$LOGS" -- make lint
ln -s "$MAIN" "${WORK}/main-link"
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: worktree not under' --worktree "${WORK}/main-link" --logs "$LOGS" -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: worktree not a directory' --worktree "${MAIN}/.worktrees/nope" --logs "$LOGS" -- make lint

# --- logs dir validation
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: logs dir must be absolute' --worktree "$WT" --logs logs -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 "REFUSED: logs dir contains '..'" --worktree "$WT" --logs "${WORK}/x/../logs" -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: logs dir inside the worktree' --worktree "$WT" --logs "${WT}/logs" -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: logs dir under the main checkout' --worktree "$WT" --logs "${MAIN}/logs" -- make lint
GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: logs dir under the main checkout' --worktree "$WT" --logs "${MAIN}/.worktrees/other/logs" -- make lint
[[ ! -e "${MAIN}/logs" ]] || fail "logs dir in the main checkout was created"
[[ ! -e "${WT}/logs" ]] || fail "logs dir inside the worktree was created"

# --- argument validation
refused --repeat 0 -- make lint
refused --repeat 21 -- make lint
refused --repeat '1;id' -- make lint
refused --name 'x/y' -- make lint
refused --bogus -- make lint
refused --

# --- shell metacharacters (the command is never parsed by a shell, but refuse anyway)
refused -- make lint '&&' id
refused -- make lint '&' id
refused -- make 'lint;id'
refused -- make lint '|' id
refused -- make "lint"$'\n'"id"
refused -- make lint 'TEST=$HOME'
refused -- make lint 'TEST=`id`'
refused -- make lint 'TEST=$(id)'
refused -- make lint 'TEST=a>b'
refused -- uv run --package joustmania-menu pytest 'tests/x.py::t"q'

# --- interpreters and command escapes
refused -- uv run bash -c id
refused -- uv run --package joustmania-menu bash -c id
refused -- uv run --package joustmania-menu python -c 'import os'
refused -- uv run --with evil --package joustmania-menu pytest
refused -- uv run pytest
refused -- uv run --package joustmania-nope pytest
refused -- uv run --package '../x' pytest
refused -- uv pip install evil
refused -- bash -c id
refused -- sh scripts/tests/test-pass.sh
refused -- bash scripts/ci/local-gates.sh
refused -- bash scripts/tests/test-link.sh
# symlinked scripts/tests directory resolving outside the worktree
WT2="${MAIN}/.worktrees/wt2"
git -C "${MAIN}" worktree add -q "${WT2}" -b wt2
mkdir -p "${WT2}/scripts" "${WORK}/evil"
echo "echo pwned" >"${WORK}/evil/test-x.sh"
ln -s "${WORK}/evil" "${WT2}/scripts/tests"
GATE_RUN_DRY_RUN=1 run_gr 2 'resolves outside scripts/tests' --worktree "$WT2" --logs "$LOGS" -- scripts/tests/test-x.sh
refused -- scripts/tests/test-missing.sh
refused -- npx vitest run
refused -- docker build .
refused -- git status
refused -- env make lint

# --- tool exec hooks
refused --dir services/agent -- go test -exec /bin/sh ./...
refused --dir services/agent -- go test -exec=sh ./...
refused --dir services/agent -- go test -toolexec sh ./...
refused --dir services/agent -- go vet -vettool=x ./...
refused --dir services/agent -- go run ./cmd
refused --dir services/agent -- go generate ./...
refused -- go test ./...
refused --dir services/rust-hid -- cargo test --config 'build.rustc-wrapper=x'
refused --dir services/rust-hid -- cargo test --manifest-path x/Cargo.toml
refused --dir services/rust-hid -- cargo run
refused --dir services -- cargo test

# --- make: targets, -f / SHELL= / unlisted vars / bad values
refused -- make -f evil.mk lint
refused -- make lint -f evil.mk
refused -- make -C /tmp lint
refused -- make lint SHELL=/bin/sh
refused -- make lint 'SHELL=sh'
refused -- make test-integration 'PYTEST_EXTRA=x SHELL=y'
refused -- make lint MAKEFLAGS=x
refused -- make lint FOO=bar
refused -- make up
refused -- make protos
refused -- make specs
refused -- make test-debug
refused -- make down
refused -- make
refused --dir services -- make lint
refused -- make pr-gates 'SERVICE=menu agent'
refused -- make pr-gates SERVICE=Menu
refused -- make pr-gates 'BASE=main x'
refused -- make pr-gates BASE=--output=x
refused -- make pr-gates 'BASE=a;b'
refused -- make test-service 'TEST=a$b'
refused -- make test-service SERVICE_TESTS=../x
refused -- make test-service SERVICE_TESTS=lib/x.py
refused -- make test-service 'SERVICE_TESTS=tests/service /etc/x'
refused -- make test-service SERVICE_TESTS=tests/servicefoo
refused -- make test-service 'SERVICE_TESTS=tests/service --pdb'
refused -- make test-service SERVICE_TESTS=
refused -- make test-service SERVICE_TESTS=tests/service/../../x
refused -- make test-service SERVICE_TESTS=/etc/passwd

# --- paths outside the worktree
refused -- uv run --package joustmania-menu pytest /tmp/evil_test.py
refused -- uv run --package joustmania-menu pytest --rootdir=/tmp
refused -- uv run --package joustmania-menu pytest ../x
refused --dir ../wt -- make lint
refused --dir /tmp -- make lint

# --- the real gate forms are accepted
accepted -- make pr-gates
accepted -- make pr-gates SERVICE=menu,agent BASE=origin/main
accepted -- make pr-gates SERVICE=dashboard
accepted -- make pr-gates SERVICE=game_coordinator BASE=refs/review/1570
accepted -- make lint
accepted -- make check
accepted -- make specs-check
accepted -- make spec-refs-check
accepted -- make test-contract
accepted -- make test-contract-flagd
refused -- make test-integration-shard-check
accepted -- make test-service SERVICE_TESTS=tests/service/test_x.py
accepted -- make test-service 'SERVICE_TESTS=tests/service/test_a.py tests/service/test_b.py'
accepted -- make test-service TEST=tc018 'PYTEST_EXTRA=--count 3'
accepted -- make test-integration TEST=test_ffa 'PYTEST_EXTRA=--reruns 1'
refused -- make test-integration-core
refused -- make test-integration-agent
refused -- make test-integration E2E_SHARD=core
accepted -- make test-pulled IMAGE_TAG=latest
accepted -- make test PR_LABELS=ci-full
accepted -- uv run --package joustmania-menu --extra test pytest -q --tb=short 'services/menu/tests/test_x.py::test_y[a-1]' -k 'a and not b'
accepted -- uv run --package joustmania-menu pytest services/menu/tests -p no:cacheprovider
accepted --dir services/agent -- go test -race -count=3 -run '^TestDecide' ./decision/...
accepted --dir services/agent -- go vet ./...
accepted --dir services/rust-hid -- cargo clippy -- -D warnings
accepted --dir services/rust-hid -- cargo test -- --test-threads=1
accepted -- scripts/tests/test-pass.sh
accepted -- bash scripts/tests/test-pass.sh

# --- clean-test-venv would sudo rm a non-writable .venv-test
mkdir -p "${WT}/.venv-test"
chmod a-w "${WT}/.venv-test"
if [[ ! -w "${WT}/.venv-test" ]]; then  # root can write anyway; skip the check then
  GATE_RUN_DRY_RUN=1 run_gr 2 'REFUSED: .venv-test is not writable' --worktree "$WT" --logs "$LOGS" -- make test-integration
fi
chmod u+w "${WT}/.venv-test"

# --- real runs and verdicts
run_gr 0 'VERDICT: PASS' --worktree "$WT" --logs "$LOGS" --repeat 2 --name ok -- scripts/tests/test-pass.sh
NS="${LOGS}/wt-$(git -C "$WT" rev-parse --short=8 HEAD)"
[[ -f "${NS}/ok-1.log" && -f "${NS}/ok-2.log" ]] || fail "logs ok-1/ok-2 not written under ${NS}"
# #2036: two worktrees sharing one --logs dir never clobber each other, and the
# header names the worktree and head.
git -C "${MAIN}" worktree add -q "${MAIN}/.worktrees/wt3" -b wt3-ns
mkdir -p "${MAIN}/.worktrees/wt3/scripts/tests"
echo "echo second worktree" >"${MAIN}/.worktrees/wt3/scripts/tests/test-pass.sh"
run_gr 0 "gate-run ${MAIN}/.worktrees/wt3 head $(git -C "${MAIN}/.worktrees/wt3" rev-parse --short=8 HEAD) job ok" \
  --worktree "${MAIN}/.worktrees/wt3" --logs "$LOGS" --name ok -- scripts/tests/test-pass.sh
NS2="${LOGS}/wt3-$(git -C "${MAIN}/.worktrees/wt3" rev-parse --short=8 HEAD)"
grep -q "all good" "${NS}/ok-1.log" && grep -q "second worktree" "${NS2}/ok-1.log" \
  || fail "logs of two worktrees clobbered each other"
git -C "${MAIN}" worktree remove --force "${MAIN}/.worktrees/wt3"
run_gr 1 'VERDICT: FAIL' --worktree "$WT" --logs "$LOGS" -- scripts/tests/test-assert.sh
export FLAKY_COUNTER="${WORK}/flaky-counter"
run_gr 1 'VERDICT: FLAKY 2/3' --worktree "$WT" --logs "$LOGS" --repeat 3 -- scripts/tests/test-flaky.sh
rm -f "$FLAKY_COUNTER"
run_gr 0 'MUTATION CAUGHT \(assertion\)' --worktree "$WT" --logs "$LOGS" --expect-fail -- scripts/tests/test-assert.sh
run_gr 0 'note: N=1 expect-fail' --worktree "$WT" --logs "$LOGS" --expect-fail -- scripts/tests/test-assert.sh
run_gr 1 'CAUGHT\? \(non-test error' --worktree "$WT" --logs "$LOGS" --expect-fail -- scripts/tests/test-collect.sh
run_gr 1 'MUTATION NOT CAUGHT \(1/1 passed\)' --worktree "$WT" --logs "$LOGS" --expect-fail -- scripts/tests/test-pass.sh
run_gr 1 'MUTATION NOT CAUGHT \(1/2 passed\)' --worktree "$WT" --logs "$LOGS" --repeat 2 --expect-fail -- scripts/tests/test-flaky.sh
run_gr 1 'wt-[0-9a-f]{8}/test-assert-1.log:.*AssertionError' --worktree "$WT" --logs "$LOGS" --name test-assert -- scripts/tests/test-assert.sh
# #2036: a shell test's FAIL line is an assertion, not a non-test error.
run_gr 0 'MUTATION CAUGHT \(assertion\)' --worktree "$WT" --logs "$LOGS" --name shfail --expect-fail -- scripts/tests/test-shfail.sh

# --- detach / await (#2036)
export SLOW_COUNTER="${WORK}/slow-counter"
starts() { cat "$SLOW_COUNTER" 2>/dev/null || echo 0; }
# A call that cannot wait long enough: PENDING, exit 3; the same call later: the verdict.
GATE_RUN_WAIT=1 run_gr 3 'VERDICT: PENDING \([0-9]+s\); rerun the same command' \
  --worktree "$WT" --logs "$LOGS" --name slow -- scripts/tests/test-slow.sh
[[ -f "${NS}/slow.job/pid" && -f "${NS}/slow.job/cmd" && -f "${NS}/slow.job/lock" ]] || fail "job dir lacks pid/cmd/lock"
# A different command under the same name while the job runs: REFUSED.
run_gr 2 'REFUSED: .*different command' --worktree "$WT" --logs "$LOGS" --name slow -- scripts/tests/test-pass.sh
run_gr 0 'VERDICT: PASS' --worktree "$WT" --logs "$LOGS" --name slow -- scripts/tests/test-slow.sh
[[ "$(cat "${NS}/slow.job/rc")" == 0 ]] || fail "job rc not written"
# The identical call again: the finished verdict, no second job.
run_gr 0 'VERDICT: PASS' --worktree "$WT" --logs "$LOGS" --name slow -- scripts/tests/test-slow.sh
[[ "$(starts)" == 1 ]] || fail "identical call started a second job (starts: $(starts))"
# A changed worktree (e.g. the next mutation) with the same command: a fresh run.
echo mutated >"${WT}/mutant.txt"
run_gr 0 'VERDICT: PASS' --worktree "$WT" --logs "$LOGS" --name slow -- scripts/tests/test-slow.sh
[[ "$(starts)" == 2 ]] || fail "changed worktree reused the old verdict (starts: $(starts))"
rm -f "${WT}/mutant.txt" "$SLOW_COUNTER"
# A killed waiter (its whole process group, as the Bash tool does) leaves the job alive.
# The job blocks on a fifo (fd 7 read-write, so opening never blocks) until released.
mkfifo "${WORK}/slow-gate"
exec 7<>"${WORK}/slow-gate"
export SLOW_GATE="${WORK}/slow-gate"
setsid timeout 60 env GATE_RUN_WAIT=30 "${GATE_RUN}" --worktree "$WT" --logs "$LOGS" --name killed \
  -- scripts/tests/test-slow.sh >/dev/null 2>&1 &
waiter=$!
for _ in $(seq 300); do [[ -f "${NS}/killed.job/pid" ]] && break; sleep 0.1; done
[[ -f "${NS}/killed.job/pid" ]] || fail "killed-waiter job did not start"
kill -KILL -- "-${waiter}" 2>/dev/null || kill -KILL "$waiter"
wait "$waiter" 2>/dev/null || true
kill -0 "$(cat "${NS}/killed.job/pid")" 2>/dev/null || fail "job died with its waiter"
echo go >&7
# Let the job finish before the next call, so the test does not race its rc write.
for _ in $(seq 300); do [[ -f "${NS}/killed.job/rc" ]] && break; sleep 0.1; done
[[ -f "${NS}/killed.job/rc" ]] || fail "released job did not finish"
run_gr 0 'VERDICT: PASS' --worktree "$WT" --logs "$LOGS" --name killed -- scripts/tests/test-slow.sh
[[ "$(starts)" == 1 ]] || fail "killed waiter: job restarted (starts: $(starts))"
exec 7>&-
unset SLOW_GATE
# A job killed before it wrote rc is replaced by a fresh one on the next call.
rm -f "$SLOW_COUNTER"
# Blocked on a fifo nobody writes to, so the job is still running when it is killed.
mkfifo "${WORK}/dead-gate"
exec 7<>"${WORK}/dead-gate"
SLOW_GATE="${WORK}/dead-gate" GATE_RUN_WAIT=0 run_gr 3 'VERDICT: PENDING' --worktree "$WT" --logs "$LOGS" --name dead -- scripts/tests/test-slow.sh
for _ in $(seq 300); do [[ -f "${NS}/dead.job/pid" && "$(starts)" == 1 ]] && break; sleep 0.1; done
[[ "$(starts)" == 1 ]] || fail "dead job did not start"
pkill -KILL -s "$(cat "${NS}/dead.job/pid")" || true
for _ in $(seq 300); do flock -n "${NS}/dead.job/lock" true && break; sleep 0.1; done
exec 7>&-
run_gr 0 'VERDICT: PASS' --worktree "$WT" --logs "$LOGS" --name dead -- scripts/tests/test-slow.sh
[[ "$(starts)" == 2 ]] || fail "killed job was not replaced (starts: $(starts))"
# Concurrent identical calls after the worktree changed: one fresh job, not one each.
rm -f "$SLOW_COUNTER"
echo mutated >"${WT}/mutant.txt"
for _ in 1 2 3; do
  timeout 60 "${GATE_RUN}" --worktree "$WT" --logs "$LOGS" --name dead -- scripts/tests/test-slow.sh >/dev/null 2>&1 &
done
wait || true
run_gr 0 'VERDICT: PASS' --worktree "$WT" --logs "$LOGS" --name dead -- scripts/tests/test-slow.sh
[[ "$(starts)" == 1 ]] || fail "concurrent calls replaced each other's job (starts: $(starts))"
rm -f "${WT}/mutant.txt" "$SLOW_COUNTER"
# The caller's detach env (preset, or inherited from an outer gate-run job) never
# reaches the run, and the call still detaches.
GATE_RUN_WORKER=1 GATE_RUN_WAIT=30 DETACH_WAIT=30 run_gr 0 'job envleak.*VERDICT: PASS' \
  --worktree "$WT" --logs "$LOGS" --name envleak -- scripts/tests/test-env.sh
[[ -f "${NS}/envleak.job/rc" ]] || fail "preset GATE_RUN_WORKER skipped the detach"
GATE_RUN_WAIT=600 run_gr 2 'REFUSED: wait must be 0-580' --worktree "$WT" --logs "$LOGS" --name w -- scripts/tests/test-pass.sh

[[ -z "$(git -C "$WT" status --porcelain --untracked-files=no)" ]] || fail "worktree changed"
echo "test-gate-run: ${pass} checks passed"
