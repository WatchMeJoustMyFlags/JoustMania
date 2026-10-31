#!/usr/bin/env bash
# Self-contained tests for scripts/ci/review-gate.sh and .github/workflows/review-gate.yml (#1809).
#
# A stub `gh` on PATH serves PR and commit-status fixtures and records every call,
# so no network or real PR is touched. The workflow checks need python3 + PyYAML
# (as test-ci-workflow.sh).
#
#   ./scripts/tests/test-review-gate.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GATE="${REPO_ROOT}/scripts/ci/review-gate.sh"
WORKFLOW="${REPO_ROOT}/.github/workflows/review-gate.yml"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export STUB_DIR="${WORK}/stub"
mkdir -p "${STUB_DIR}" "${WORK}/bin"

HEAD=abcdef1234567890abcdef1234567890abcdef12
BASE=0123456789abcdef0123456789abcdef01234567
REF="gh-readonly-queue/main/pr-42-${BASE}"

# Stub gh: pr.json for pulls/42, statuses.json for the head's statuses; a
# pr-fail / statuses-fail file makes that read fail (API error).
cat >"${WORK}/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >>"$STUB_DIR/calls"
jqexpr=""; prev=""
for a in "$@"; do [[ "$prev" == "--jq" ]] && jqexpr="$a"; prev="$a"; done
case "$1 $2" in
  "api repos/{owner}/{repo}/pulls/"*)
    [[ -f "$STUB_DIR/pr-fail" ]] && exit 1
    jq -r "$jqexpr" "$STUB_DIR/pr.json" ;;
  "api repos/{owner}/{repo}/commits/"*"/statuses?per_page=100")
    [[ -f "$STUB_DIR/statuses-fail" ]] && exit 1
    jq -r "$jqexpr" "$STUB_DIR/statuses.json" ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
STUB
chmod +x "${WORK}/bin/gh"
export PATH="${WORK}/bin:${PATH}"

pass=0
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { pass=$((pass + 1)); }

st() {  # id state creator [context]
  printf '{"id":%s,"state":"%s","context":"%s","creator":{"login":"%s"}}' "$1" "$2" "${4:-Reviewed head}" "$3"
}
statuses() {  # entries... (GitHub order: newest first)
  local IFS=,
  printf '[%s]' "$*" >"${STUB_DIR}/statuses.json"
}
reset() {
  rm -f "${STUB_DIR}"/*
  printf '{"number":42,"head":{"sha":"%s"}}' "$HEAD" >"${STUB_DIR}/pr.json"
}
run() {  # [VAR=value ...] -> sets OUT and RC
  set +e
  OUT="$(env -u REVIEW_APPROVERS HEAD_REF="$REF" "$@" timeout 20 "${GATE}" 2>"${WORK}/stderr")"
  RC=$?
  set -e
}
calls() { cat "${STUB_DIR}/calls" 2>/dev/null || true; }

# 1. Newest "Reviewed head" is success by an approver (default aepfli): pass.
reset; statuses "$(st 900 success aepfli)" "$(st 800 success sonarcloud[bot] 'SonarCloud')"
run
[[ $RC -eq 0 && "$OUT" == "REVIEWED: PR #42 head abcdef12 by aepfli" ]] || fail "approved: rc=$RC $OUT"
calls | grep -qF "api repos/{owner}/{repo}/pulls/42 --jq .head.sha" || fail "PR read: $(calls)"
calls | grep -qF "commits/${HEAD}/statuses?per_page=100 --paginate" || fail "statuses read of the PR head: $(calls)"
# refs/heads/ prefix is accepted too.
run HEAD_REF="refs/heads/${REF}"
[[ $RC -eq 0 ]] || fail "refs/heads/ prefix: rc=$RC $OUT"
ok

# 2. No "Reviewed head" status (only other contexts, or none): fail.
reset; statuses "$(st 800 success aepfli 'CI Complete')"
run
[[ $RC -eq 1 && "$OUT" == *"NOT REVIEWED"*"no \"Reviewed head\" status"* ]] || fail "no status: rc=$RC $OUT"
reset; statuses
run
[[ $RC -eq 1 ]] || fail "empty statuses: rc=$RC $OUT"
ok

# 3. Revoke: the newest entry is failure, an older one success -> fail, whatever
#    the order the API lists them in (the highest id wins).
reset; statuses "$(st 901 failure aepfli)" "$(st 900 success aepfli)"
run
[[ $RC -eq 1 && "$OUT" == *"newest \"Reviewed head\" is failure"* ]] || fail "revoked: rc=$RC $OUT"
reset; statuses "$(st 900 success aepfli)" "$(st 901 failure aepfli)"
run
[[ $RC -eq 1 ]] || fail "revoked, listed oldest first: rc=$RC $OUT"
# Re-approved after a revoke: the newest success wins.
reset; statuses "$(st 902 success aepfli)" "$(st 901 failure aepfli)" "$(st 900 success aepfli)"
run
[[ $RC -eq 0 ]] || fail "re-approved after revoke: rc=$RC $OUT"
# pending / error never pass.
for s in pending error; do
  reset; statuses "$(st 900 "$s" aepfli)"
  run
  [[ $RC -eq 1 ]] || fail "state $s: rc=$RC $OUT"
done
ok

# 4. Creator: github-actions[bot] never passes, even when listed; someone outside
#    REVIEW_APPROVERS fails; a listed approver passes (comma or space separated).
reset; statuses "$(st 900 success 'github-actions[bot]')"
run REVIEW_APPROVERS='aepfli,github-actions[bot]'
[[ $RC -eq 1 && "$OUT" == *"posted by a bot"* ]] || fail "github-actions creator: rc=$RC $OUT"
reset; statuses "$(st 900 success mallory)"
run
[[ $RC -eq 1 && "$OUT" == *"not in REVIEW_APPROVERS (aepfli)"* ]] || fail "outsider: rc=$RC $OUT"
run REVIEW_APPROVERS='alice, mallory'
[[ $RC -eq 0 ]] || fail "listed approver: rc=$RC $OUT"
# An empty (unset repo variable) REVIEW_APPROVERS falls back to aepfli, never to "anyone".
run REVIEW_APPROVERS=''
[[ $RC -eq 1 ]] || fail "empty REVIEW_APPROVERS must default to aepfli: rc=$RC $OUT"
# A partial login match does not count.
reset; statuses "$(st 900 success aepfli2)"
run
[[ $RC -eq 1 ]] || fail "partial login match: rc=$RC $OUT"
ok

# 5. Malformed head_ref: fail closed, no API call.
for ref in '' 'main' 'gh-readonly-queue/main/pr-x-'"$BASE" 'gh-readonly-queue/main/pr-42' \
  'gh-readonly-queue/release/pr-42-'"$BASE" 'gh-readonly-queue/main/pr-42-'"$BASE"'x' 'feature/pr-42-'"$BASE"; do
  reset; statuses "$(st 900 success aepfli)"
  run HEAD_REF="$ref"
  [[ $RC -eq 1 && "$OUT" == *"cannot parse"* ]] || fail "ref '$ref': rc=$RC $OUT"
  [[ -z "$(calls)" ]] || fail "ref '$ref' must not call the API: $(calls)"
done
ok

# 6. API errors and unreadable answers: fail closed.
reset; statuses "$(st 900 success aepfli)"; touch "${STUB_DIR}/pr-fail"
run
[[ $RC -eq 1 && "$OUT" == *"API error"* ]] || fail "PR read fails: rc=$RC $OUT"
reset; statuses "$(st 900 success aepfli)"; touch "${STUB_DIR}/statuses-fail"
run
[[ $RC -eq 1 && "$OUT" == *"API error"* ]] || fail "statuses read fails: rc=$RC $OUT"
reset; statuses "$(st 900 success aepfli)"; printf '{"number":42,"head":{"sha":null}}' >"${STUB_DIR}/pr.json"
run
[[ $RC -eq 1 ]] || fail "PR without head sha: rc=$RC $OUT"
ok

# 7. The workflow: merge_group only, one job named exactly "Reviewed head" that runs
#    the gate, read-only permissions, no continue-on-error, and ci.yml never
#    depends on it (CI Complete also runs on PR heads).
python3 - "$WORKFLOW" "${REPO_ROOT}/.github/workflows/ci.yml" <<'PY'
import sys

import yaml

with open(sys.argv[1]) as fh:
    wf = yaml.safe_load(fh)
failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


on = wf.get("on", wf.get(True))
check(isinstance(on, dict) and list(on) == ["merge_group"], f"on: must be merge_group only, got {on!r}")
check((on or {}).get("merge_group") == {"types": ["checks_requested"]}, "merge_group types: [checks_requested]")
perms = wf.get("permissions") or {}
check(isinstance(perms, dict) and perms and all(v == "read" for v in perms.values()),
      f"top-level permissions must all be read, got {perms!r}")
check(set(perms) <= {"contents", "statuses", "pull-requests"}, f"unexpected permission scopes {sorted(perms)}")
jobs = wf.get("jobs") or {}
check(len(jobs) == 1, f"exactly one job, got {sorted(jobs)}")
for jid, job in jobs.items():
    check(job.get("name") == "Reviewed head", f"job {jid} name must be exactly 'Reviewed head'")
    check("permissions" not in job, f"job {jid} must not widen permissions")
    check("continue-on-error" not in job, f"job {jid}: no continue-on-error")
    check("if" not in job, f"job {jid}: no if: (a skipped job could satisfy the check)")
    steps = job.get("steps") or []
    for st in steps:
        check("continue-on-error" not in st, f"job {jid} step {st.get('name')}: no continue-on-error")
    runs = [st for st in steps if "review-gate.sh" in str(st.get("run", ""))]
    check(len(runs) == 1, f"job {jid} must run scripts/ci/review-gate.sh once")
    if runs:
        env = runs[0].get("env") or {}
        check(env.get("HEAD_REF") == "${{ github.event.merge_group.head_ref }}", "HEAD_REF from merge_group.head_ref")
        check(env.get("REVIEW_APPROVERS") == "${{ vars.REVIEW_APPROVERS }}", "REVIEW_APPROVERS from vars")
        check(env.get("GH_TOKEN") == "${{ github.token }}", "GH_TOKEN is the read-only github.token")
with open(sys.argv[2]) as fh:
    ci_text = fh.read()
check("Reviewed head" not in ci_text, "ci.yml must not reference the 'Reviewed head' check")
ci = yaml.safe_load(ci_text)
for jid, job in (ci.get("jobs") or {}).items():
    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else needs
    check("reviewed-head" not in needs, f"ci.yml job {jid} must not need reviewed-head")
if failures:
    print("FAIL: review-gate.yml: " + "; ".join(failures), file=sys.stderr)
    sys.exit(1)
PY
ok

echo "test-review-gate: ${pass} groups passed"
