#!/usr/bin/env bash
# Tests for scripts/ci/validate-agent-protos.sh + gen-agent-protos.sh (#1801):
# the committed agent Go stubs must match a fresh generation, and every kind of
# drift (modified, deleted, untracked) fails. A scratch git repo and a fake
# docker whose "buf generate" writes a fixed set of files, so no network.
# UC: none (CI-only)
#
#   ./scripts/tests/test-validate-agent-protos.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
pass=0

fail() { echo "FAILED: $*" >&2; exit 1; }

# What the fake `buf generate` produces (the "fresh generation").
FRESH="${WORK}/fresh"
mkdir -p "${FRESH}/game_coordinator" "${FRESH}/controller_manager_mock"
printf 'package game_coordinator\n// generated\n' >"${FRESH}/game_coordinator/game_coordinator.pb.go"
printf 'package game_coordinator\n// grpc\n' >"${FRESH}/game_coordinator/game_coordinator_grpc.pb.go"
printf 'package controller_manager_mock\n// generated\n' >"${FRESH}/controller_manager_mock/controller_manager_mock.pb.go"

# Fake docker: `image inspect` says the image is local; `run` copies FRESH into
# the dir mounted at /out under the go_package layout. FAKE_DOCKER_FAIL=1 makes
# every run fail; each run is logged to FAKE_DOCKER_LOG.
BIN="${WORK}/bin"
mkdir -p "${BIN}"
cat >"${BIN}/docker" <<'SH'
#!/usr/bin/env bash
echo "$*" >>"${FAKE_DOCKER_LOG}"
case "$1" in
  image|pull) exit 0 ;;
  run)
    [[ "${FAKE_DOCKER_FAIL:-0}" == 1 ]] && { echo "fake: buf.build unreachable" >&2; exit 1; }
    out=""
    prev=""
    for a in "$@"; do
      if [[ "$prev" == "-v" && "$a" == *:/out ]]; then out="${a%:/out}"; fi
      prev="$a"
    done
    [[ -n "$out" ]] || { echo "fake docker: no /out mount" >&2; exit 1; }
    mkdir -p "$out/github.com/joustmania/agent/gen"
    cp -R "${FAKE_GEN}/." "$out/github.com/joustmania/agent/gen/"
    ;;
  *) echo "fake docker: unexpected $*" >&2; exit 1 ;;
esac
SH
chmod +x "${BIN}/docker"

# new_repo <name>: a scratch repo with both scripts and the committed stubs = FRESH.
new_repo() {
  local repo="${WORK}/$1"
  mkdir -p "${repo}/scripts/ci" "${repo}/proto" "${repo}/services/agent/gen"
  cp "${REPO_ROOT}/scripts/ci/validate-agent-protos.sh" "${REPO_ROOT}/scripts/ci/gen-agent-protos.sh" "${repo}/scripts/ci/"
  cp -R "${FRESH}/." "${repo}/services/agent/gen/"
  git -C "${repo}" init -q
  git -C "${repo}" add -A
  git -C "${repo}" -c user.name=t -c user.email=t@t commit -qm init
  echo "${repo}"
}

commit_all() { git -C "$1" add -A && git -C "$1" -c user.name=t -c user.email=t@t commit -qm mutate; }

# expect <rc> <desc> <repo> [env...]: run the validator, check its exit code.
expect() {
  local want="$1" desc="$2" repo="$3"; shift 3
  local rc=0
  env PATH="${BIN}:${PATH}" FAKE_GEN="${FRESH}" FAKE_DOCKER_LOG="${WORK}/docker.log" \
    AGENT_PROTOS_RETRY_DELAYS="0 0" "$@" \
    bash "${repo}/scripts/ci/validate-agent-protos.sh" >"${WORK}/out.log" 2>&1 || rc=$?
  [[ "$rc" == "$want" ]] || { cat "${WORK}/out.log" >&2; fail "${desc}: exit ${rc}, want ${want}"; }
  pass=$((pass + 1))
}

# 1. Clean: committed stubs equal the fresh generation.
r="$(new_repo clean)"
expect 0 "clean tree" "$r"
[[ -z "$(git -C "$r" status --porcelain)" ]] || fail "clean tree: validation left changes behind"
pass=$((pass + 1))

# 2. M: a hand-edited generated file (#1790).
r="$(new_repo modified)"
printf '// hand edit\n' >>"${r}/services/agent/gen/game_coordinator/game_coordinator.pb.go"
commit_all "$r"
expect 1 "hand-edited stub" "$r"
grep -q '^Files .*game_coordinator/game_coordinator.pb.go .* differ' "${WORK}/out.log" || fail "modified: differ line missing"
grep -q "make protos-agent" "${WORK}/out.log" || fail "modified: fix hint missing"
pass=$((pass + 2))

# 3. ??: a generated file never committed (e.g. a new proto's stubs).
r="$(new_repo untracked)"
git -C "$r" rm -q services/agent/gen/controller_manager_mock/controller_manager_mock.pb.go
commit_all "$r"
expect 1 "stub missing from the commit" "$r"
grep -q '^Only in /.*: controller_manager_mock$' "${WORK}/out.log" || fail "untracked: only-in-fresh line missing"
pass=$((pass + 1))

# 4. D: a committed file the generation no longer produces (stale stub).
r="$(new_repo deleted)"
mkdir -p "${r}/services/agent/gen/old"
printf 'package old\n' >"${r}/services/agent/gen/old/old.pb.go"
commit_all "$r"
expect 1 "stale committed stub" "$r"
grep -q '^Only in services/agent/gen: old$' "${WORK}/out.log" || fail "deleted: only-in-tree line missing"
pass=$((pass + 1))

# 5. Generation failure is exit 2 (not drift), after 3 attempts, gen dir untouched.
r="$(new_repo genfail)"
: >"${WORK}/docker.log"
expect 2 "generation failure" "$r" FAKE_DOCKER_FAIL=1
[[ "$(grep -c '^run ' "${WORK}/docker.log")" == 3 ]] || fail "generation failure: want 3 buf runs, got $(grep -c '^run ' "${WORK}/docker.log")"
grep -q "generation failed (network, image, or proto/template error); see log above" "${WORK}/out.log" || fail "generation failure: neutral message missing"
[[ -z "$(git -C "$r" status --porcelain)" ]] || fail "generation failure: committed stubs must be left untouched"
pass=$((pass + 3))

# 5b. Dirty tree: a correct but uncommitted regeneration passes; an uncommitted
# hand edit fails and is NOT overwritten.
r="$(new_repo dirty)"
printf 'package game_coordinator\n// old committed\n' >"${r}/services/agent/gen/game_coordinator/game_coordinator.pb.go"
commit_all "$r"
cp -R "${FRESH}/." "${r}/services/agent/gen/"
expect 0 "uncommitted correct regeneration" "$r"
r="$(new_repo dirty-edit)"
printf '// uncommitted hand edit\n' >>"${r}/services/agent/gen/game_coordinator/game_coordinator.pb.go"
expect 1 "uncommitted hand edit" "$r"
grep -q 'uncommitted hand edit' "${r}/services/agent/gen/game_coordinator/game_coordinator.pb.go" || fail "dirty tree: uncommitted edit was overwritten"
pass=$((pass + 3))

# 6. The buf invocation keeps its pins: image tag and template.
grep -q 'bufbuild/buf:1.47.2 generate --template buf.gen.agent.yaml' "${WORK}/docker.log" || fail "buf image/template pin changed"
pass=$((pass + 1))

# 7. The template pins both remote plugins by version and revision.
python3 - "${REPO_ROOT}/proto/buf.gen.agent.yaml" <<'PY' || fail "proto/buf.gen.agent.yaml: every remote plugin needs a revision"
import sys, yaml
plugins = yaml.safe_load(open(sys.argv[1]))["plugins"]
assert plugins and all("remote" in p and ":" in p["remote"] and isinstance(p.get("revision"), int) for p in plugins), plugins
PY
pass=$((pass + 1))

echo "PASS: ${pass} validate-agent-protos checks"
