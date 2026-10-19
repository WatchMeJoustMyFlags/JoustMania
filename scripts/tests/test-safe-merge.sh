#!/usr/bin/env bash
# Self-contained tests for scripts/ci/safe-merge.sh.
#
# A stub `gh` on PATH serves fixture JSON and records every call, so no
# network or real PR is touched. Polling is set to 0 s (SAFE_MERGE_TEST=1
# lifts the 60 s floor), and every helper run is capped by `timeout`.
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
export SAFE_MERGE_TEST=1 SAFE_MERGE_POLL=0 SAFE_MERGE_SETTLE=0 SAFE_MERGE_MAX_WAIT=5

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
  "run rerun") echo x >>"$STUB_DIR/reruns"; [[ -f "$STUB_DIR/view-after-rerun.json" ]] && mv "$STUB_DIR/view-after-rerun.json" "$STUB_DIR/view.json"; exit 0 ;;
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
status_ctx() {  # context state
  printf '{"__typename":"StatusContext","context":"%s","state":"%s"}' "$1" "$2"
}
view() {  # checks...
  local IFS=,
  printf '{"headRefOid":"%s","state":"OPEN","statusCheckRollup":[%s]}' "$HEAD" "$*" >"${STUB_DIR}/view.json"
}
after_merge() {  # state head merge_sha
  printf '{"state":"%s","headRefOid":"%s","url":"https://github.com/o/r/pull/42","mergeCommit":%s}' \
    "$1" "$2" "$( [[ -n "$3" ]] && printf '{"oid":"%s"}' "$3" || echo null)" >"${STUB_DIR}/after-merge.json"
}
reset() {
  rm -f "${STUB_DIR}"/*
  after_merge MERGED "$HEAD" "$MERGE_SHA"
}
run() {  # args... -> sets OUT and RC (a looping helper fails the test via timeout)
  set +e
  OUT="$(timeout 20 "${HELPER}" "$@" 2>"${WORK}/stderr")"
  RC=$?
  set -e
}
calls() { cat "${STUB_DIR}/calls" 2>/dev/null || true; }

echo 'Merged in {MERGE}, thanks.' >"${WORK}/comment.md"
echo 'Follow-up landed in {MERGE}.' >"${WORK}/note.md"
GREEN="$(check 'Lint' COMPLETED SUCCESS 100),$(check 'CI Complete' COMPLETED SUCCESS 100)"

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
after_merge OPEN "$HEAD" ""
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
printf '{"headRefOid":"%s","state":"OPEN","statusCheckRollup":[%s,%s]}' "$HEAD" \
  "$(check 'Integration Tests' COMPLETED SUCCESS 300)" "$(check 'CI Complete' COMPLETED SUCCESS 300)" \
  >"${STUB_DIR}/view-after-rerun.json"
run 42 abcdef1 --rerun-failed-once
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"*"after one rerun"* ]] || fail "rerun: rc=$RC $OUT"
calls | grep -q 'run rerun 300 --failed' || fail "rerun call: $(calls)"
ok

# 7. Usage errors, rejected before any gh call.
reset
run 42
[[ $RC -eq 2 ]] || fail "usage: exit $RC"
run 42 abcdef1 --close 7
[[ $RC -eq 2 ]] || fail "--close without --comment-file: exit $RC"
for bad in '7;id' '7,' '*' '7 8' '' ','; do
  run 42 abcdef1 --close "$bad" --comment-file "${WORK}/comment.md"
  [[ $RC -eq 2 ]] || fail "--close '$bad' must be rejected: exit $RC ($OUT)"
done
for bad in '9,10' '9x' '$(id)'; do
  run 42 abcdef1 --note-issue "$bad" --note-file "${WORK}/note.md"
  [[ $RC -eq 2 ]] || fail "--note-issue '$bad' must be rejected: exit $RC ($OUT)"
done
run 42 abcdef1 --close
[[ $RC -eq 2 ]] || fail "--close without a value: exit $RC"
[[ -z "$(calls)" ]] || fail "usage errors must not call gh: $(calls)"
ok

# 8. Pending checks: keep waiting (timeout), never merge while CI runs.
reset; SAFE_MERGE_MAX_WAIT=0
view "$(check 'Lint' COMPLETED SUCCESS 100)" "$(check 'Integration Tests' IN_PROGRESS '' 100)" \
  "$(check 'CI Complete' QUEUED '' 100)"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 4 && "$OUT" == *"TIMEOUT"*"Integration Tests"* ]] || fail "pending: rc=$RC $OUT"
calls | grep -qE 'pr merge|issue' && fail "pending CI must not merge or touch issues: $(calls)"
# A pending status context counts too.
reset; view "$GREEN" "$(status_ctx 'ci/external' PENDING)"
run 42 abcdef1
[[ $RC -eq 4 ]] || fail "pending status context: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "pending status context must not merge: $(calls)"
SAFE_MERGE_MAX_WAIT=5
ok

# 9. Empty rollup / only Sonar checks / no CI Complete: never green.
reset; SAFE_MERGE_MAX_WAIT=0; view
run 42 abcdef1 --wait-ci
[[ $RC -eq 4 && "$OUT" != *"GREEN"* ]] || fail "empty rollup: rc=$RC $OUT"
reset; view "$(check 'SonarCloud Analysis' COMPLETED SUCCESS 100)"
run 42 abcdef1
[[ $RC -eq 4 ]] || fail "sonar-only rollup: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "sonar-only rollup must not merge: $(calls)"
reset; view "$(check 'Lint' COMPLETED SUCCESS 100)"
run 42 abcdef1
[[ $RC -eq 4 ]] || fail "no CI Complete: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "missing CI Complete must not merge: $(calls)"
SAFE_MERGE_MAX_WAIT=5
reset; view "$(check 'Lint' COMPLETED SUCCESS 100)" "$(check 'CI Complete' COMPLETED SKIPPED 100)" \
  "$(check 'Integration Tests' COMPLETED SUCCESS 100)"
run 42 abcdef1
[[ $RC -eq 1 && "$OUT" == *"CI Complete is SKIPPED"* ]] || fail "CI Complete skipped: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "CI Complete SKIPPED must not merge: $(calls)"
ok

# 10. #1527: GitHub counts only the newest run per workflow. A newer label-event
#     run (every job SKIPPED, no "CI Complete") blocks the PR even when the
#     real run is green, so it is NOT GREEN with push advice, never merged.
reset
view "$(check 'Integration Tests' COMPLETED FAILURE 200)" "$(check 'CI Complete' COMPLETED FAILURE 200)" \
  "$(check 'Lint' COMPLETED SUCCESS 200)" \
  "$(check 'Integration Tests' COMPLETED SKIPPED 300)" "$(check 'Label event (no CI result)' COMPLETED SKIPPED 300)" \
  "$(check 'Lint' COMPLETED NEUTRAL 300)"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 1 && "$OUT" == *"NOT GREEN"*"newest run 300 has no CI Complete"*"push a commit"* ]] || fail "label run over red: rc=$RC $OUT"
calls | grep -qE 'pr merge|issue' && fail "label run over red must not merge: $(calls)"
reset
view "$GREEN" "$(check 'Lint' COMPLETED SKIPPED 300)" "$(check 'Label event (no CI result)' COMPLETED SKIPPED 300)"
run 42 abcdef1
[[ $RC -eq 1 && "$OUT" == *"NOT GREEN"*"newest run 300"* ]] || fail "label run over green: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "label run over green must not merge (GitHub blocks it): $(calls)"
# A skipped-only run of ANOTHER workflow (Spec label re-run) does not block.
reset
view "$GREEN" "$(check 'Re-run Spec Gate' COMPLETED SKIPPED 300 'Spec label re-run')"
run 42 abcdef1
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"* ]] || fail "skipped other workflow: rc=$RC $OUT"
ok

# 11. Already MERGED: report and stop; never merge again or touch issues.
reset
printf '{"headRefOid":"%s","state":"MERGED","statusCheckRollup":[%s]}' "$HEAD" "$GREEN" >"${STUB_DIR}/view.json"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md" --note-issue 9 --note-file "${WORK}/note.md"
[[ $RC -eq 0 && "$OUT" == *"already MERGED"*"nothing done"* ]] || fail "already merged: rc=$RC $OUT"
calls | grep -qE 'pr merge|issue' && fail "already merged must not merge or touch issues: $(calls)"
ok

# 12. Failing status context and CANCELLED check runs are red.
reset; view "$GREEN" "$(status_ctx 'ci/external' FAILURE)"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 1 && "$OUT" == *"CI RED"*"ci/external"* ]] || fail "status context failure: rc=$RC $OUT"
calls | grep -qE 'pr merge|issue' && fail "failed status context must not merge: $(calls)"
reset; view "$GREEN" "$(status_ctx 'ci/external' ERROR)"
run 42 abcdef1
[[ $RC -eq 1 ]] || fail "status context error: rc=$RC $OUT"
reset; view "$GREEN" "$(check 'Build' COMPLETED CANCELLED 100)"
run 42 abcdef1
[[ $RC -eq 1 && "$OUT" == *"CI RED"*"Build"* ]] || fail "cancelled check: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "cancelled check must not merge: $(calls)"
ok

# 13. --rerun-failed-once reruns at most once: still red afterwards -> red.
reset; view "$(check 'Integration Tests' COMPLETED FAILURE 300)" "$(check 'CI Complete' COMPLETED FAILURE 300)"
run 42 abcdef1 --rerun-failed-once
[[ $RC -eq 1 && "$OUT" == *"CI RED"* ]] || fail "rerun once: rc=$RC $OUT"
[[ "$(wc -l <"${STUB_DIR}/reruns")" -eq 1 ]] || fail "rerun must happen exactly once: $(calls)"
ok

# 14. gh pr merge fails but the PR reads MERGED: the merged head must match.
reset; view "$GREEN"; echo 1 >"${STUB_DIR}/merge-rc"
after_merge MERGED 1111111111111111111111111111111111111111 "$MERGE_SHA"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 3 && "$OUT" == *"no issue changes"* ]] || fail "merged at other head: rc=$RC $OUT"
calls | grep -q 'issue' && fail "merged at other head must not touch issues: $(calls)"
# Same head, merge commit unknown: {MERGE} becomes the PR URL, never empty.
reset; view "$GREEN"; echo 1 >"${STUB_DIR}/merge-rc"
after_merge MERGED "$HEAD" ""
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 0 && "$OUT" == *"closed #7"* ]] || fail "merged without merge sha: rc=$RC $OUT"
calls | grep -q 'issue close 7 --comment Merged in https://github.com/o/r/pull/42, thanks.' \
  || fail "empty merge sha must fall back to the PR URL: $(calls)"
ok

echo "test-safe-merge: ${pass} groups passed"
