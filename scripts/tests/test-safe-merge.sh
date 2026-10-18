#!/usr/bin/env bash
# Self-contained tests for scripts/ci/safe-merge.sh.
#
# A stub `gh` on PATH serves fixture JSON and records every call, so no
# network or real PR is touched. Polling is set to 0 s.
#
# Runs as part of `make lint` (needs bash, jq, python3-free), or directly:
#   ./scripts/tests/test-safe-merge.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/../ci/safe-merge.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export STUB_DIR="${WORK}/stub"
mkdir -p "${STUB_DIR}" "${WORK}/bin"
export SAFE_MERGE_POLL=0 SAFE_MERGE_SETTLE=0 SAFE_MERGE_MAX_WAIT=5

HEAD=abcdef1234567890abcdef1234567890abcdef12
MERGE_SHA=9876543210fedcba9876543210fedcba98765432

# Stub gh: answers from files in $STUB_DIR, logs each call to $STUB_DIR/calls.
cat >"${WORK}/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >>"$STUB_DIR/calls"
case "$1 $2" in
  "pr view")
    if [[ "$*" == *"statusCheckRollup"* ]]; then cat "$STUB_DIR/view.json"; exit 0; fi
    jqexpr=""; prev=""
    for a in "$@"; do [[ "$prev" == "--jq" ]] && jqexpr="$a"; prev="$a"; done
    jq -r "$jqexpr" "$STUB_DIR/after-merge.json" ;;
  "pr merge") exit "$(cat "$STUB_DIR/merge-rc" 2>/dev/null || echo 0)" ;;
  "run view") cat "$STUB_DIR/log.txt" 2>/dev/null ;;
  "run rerun") [[ -f "$STUB_DIR/view-after-rerun.json" ]] && mv "$STUB_DIR/view-after-rerun.json" "$STUB_DIR/view.json"; exit 0 ;;
  "issue close"|"issue comment") exit 0 ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
STUB
chmod +x "${WORK}/bin/gh"
export PATH="${WORK}/bin:${PATH}"

pass=0
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { pass=$((pass + 1)); }

check() {  # name status conclusion run_id [workflow]
  printf '{"__typename":"CheckRun","name":"%s","status":"%s","conclusion":"%s","workflowName":"%s","detailsUrl":"https://github.com/o/r/actions/runs/%s/job/1"}' \
    "$1" "$2" "$3" "${5:-CI}" "$4"
}
view() {  # checks...
  local IFS=,
  printf '{"headRefOid":"%s","state":"OPEN","statusCheckRollup":[%s]}' "$HEAD" "$*" >"${STUB_DIR}/view.json"
}
reset() {
  rm -f "${STUB_DIR}"/*
  echo '{"state":"MERGED","mergeCommit":{"oid":"'"$MERGE_SHA"'"}}' >"${STUB_DIR}/after-merge.json"
}
run() {  # args... -> sets OUT and RC
  set +e
  OUT="$("${HELPER}" "$@" 2>"${WORK}/stderr")"
  RC=$?
  set -e
}
calls() { cat "${STUB_DIR}/calls" 2>/dev/null || true; }

echo 'Merged in {MERGE}, thanks.' >"${WORK}/comment.md"
echo 'Follow-up landed in {MERGE}.' >"${WORK}/note.md"
GREEN="$(check 'Lint' COMPLETED SUCCESS 100)"

# 1. Head mismatch: no merge, no issue changes.
reset; view "$GREEN"
run 42 1111111 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 3 ]] || fail "head mismatch: exit $RC, want 3 ($OUT)"
[[ "$OUT" == *"does not match 1111111"* ]] || fail "head mismatch line: $OUT"
calls | grep -qE 'pr merge|issue' && fail "head mismatch must not merge or touch issues: $(calls)"
ok

# 2. Merge fails (state stays OPEN): no issue changes.
reset; view "$GREEN"
echo 1 >"${STUB_DIR}/merge-rc"
echo '{"state":"OPEN","mergeCommit":null}' >"${STUB_DIR}/after-merge.json"
run 42 abcdef1 --close 7,8 --comment-file "${WORK}/comment.md" --note-issue 9 --note-file "${WORK}/note.md"
[[ $RC -eq 1 ]] || fail "merge failure: exit $RC, want 1 ($OUT)"
[[ "$OUT" == *"MERGE FAILED"*"no issue changes"* ]] || fail "merge failure line: $OUT"
calls | grep -q 'pr merge 42 --squash --match-head-commit '"$HEAD" || fail "merge not attempted at full head: $(calls)"
calls | grep -q 'issue' && fail "failed merge must not touch issues: $(calls)"
ok

# 3. Success: {MERGE} becomes the short merge SHA in close comments and notes.
reset; view "$GREEN" "$(check 'SonarCloud Analysis' COMPLETED FAILURE 100)"
run 42 abcdef12 --close 7,8 --comment-file "${WORK}/comment.md" --note-issue 9 --note-file "${WORK}/note.md"
[[ $RC -eq 0 ]] || fail "success: exit $RC ($OUT; $(cat "${WORK}/stderr"))"
[[ "$OUT" == "#42 MERGED as 98765432 (head abcdef12); closed #7; closed #8; noted #9" ]] || fail "success line: $OUT"
calls | grep -q 'issue close 7 --comment Merged in 98765432, thanks.' || fail "close #7 comment: $(calls)"
calls | grep -q 'issue close 8 --comment Merged in 98765432, thanks.' || fail "close #8 comment: $(calls)"
calls | grep -q 'issue comment 9 --body Follow-up landed in 98765432.' || fail "note #9 body: $(calls)"
calls | grep -q '{MERGE}' && fail "placeholder left unsubstituted: $(calls)"
ok

# 4. A cancelled duplicate run (older run id) whose CI Complete failed is ignored.
reset
view "$(check 'CI Complete' COMPLETED FAILURE 100)" "$(check 'Lint' COMPLETED CANCELLED 100)" \
  "$(check 'CI Complete' COMPLETED SUCCESS 200)" "$(check 'Lint' COMPLETED SUCCESS 200)"
run 42 abcdef1 --wait-ci
[[ $RC -eq 0 && "$OUT" == *"CI GREEN"*"not merged"* ]] || fail "superseded run: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "--wait-ci must never merge: $(calls)"
ok

# 5. Red CI with a stale-image signature: advice says ci-full, nothing merged.
reset; view "$(check 'Integration Tests' COMPLETED FAILURE 300)"
echo '::error::Fast-path freshness guard FAILED. The pulled :latest images are stale' >"${STUB_DIR}/log.txt"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 1 && "$OUT" == *"infra=stale-prebuilt-images"*"ci-full"* ]] || fail "stale image: rc=$RC $OUT"
calls | grep -qE 'pr merge|issue' && fail "red CI must not merge or touch issues: $(calls)"
reset; view "$(check 'Integration Tests' COMPLETED FAILURE 300)"
echo 'otel-collector-1 exited (1)  error decoding: unknown type: "span_metrics"' >"${STUB_DIR}/log.txt"
run 42 abcdef1 --wait-ci
[[ $RC -eq 1 && "$OUT" == *"infra=stale-collector-image"* ]] || fail "collector: rc=$RC $OUT"
reset; view "$(check 'Build' COMPLETED FAILURE 300)"
echo 'failed to copy: blob not found' >"${STUB_DIR}/log.txt"
run 42 abcdef1 --wait-ci
[[ $RC -eq 1 && "$OUT" == *"infra=registry-blob-not-found"*"rerun"* ]] || fail "blob: rc=$RC $OUT"
ok

# 6. --rerun-failed-once reruns the failed run, then merges once green.
reset; view "$(check 'Integration Tests' COMPLETED FAILURE 300)"
printf '{"headRefOid":"%s","state":"OPEN","statusCheckRollup":[%s]}' "$HEAD" "$(check 'Integration Tests' COMPLETED SUCCESS 300)" \
  >"${STUB_DIR}/view-after-rerun.json"
run 42 abcdef1 --rerun-failed-once
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"*"after one rerun"* ]] || fail "rerun: rc=$RC $OUT"
calls | grep -q 'run rerun 300 --failed' || fail "rerun call: $(calls)"
ok

# 7. Usage errors.
run 42
[[ $RC -eq 2 ]] || fail "usage: exit $RC"
run 42 abcdef1 --close 7
[[ $RC -eq 2 ]] || fail "--close without --comment-file: exit $RC"
ok

echo "test-safe-merge: ${pass} groups passed"
