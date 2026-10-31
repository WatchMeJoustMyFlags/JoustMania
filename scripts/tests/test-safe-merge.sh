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
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"
# Hermetic: a fail-loud fake docker first on PATH, no COMPOSE / DOCKER override.
unset COMPOSE DOCKER
FAKE_DOCKER_DIR="$(mktemp -d)"
printf '#!/bin/sh\necho "test reached docker: $*" >&2\nexit 99\n' >"${FAKE_DOCKER_DIR}/docker"
chmod +x "${FAKE_DOCKER_DIR}/docker"
export PATH="${FAKE_DOCKER_DIR}:${PATH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="${SCRIPT_DIR}/../ci/safe-merge.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}" "${FAKE_DOCKER_DIR}"' EXIT
export STUB_DIR="${WORK}/stub"
mkdir -p "${STUB_DIR}" "${WORK}/bin"
export SAFE_MERGE_TEST=1 SAFE_MERGE_POLL=0 SAFE_MERGE_SETTLE=0 SAFE_MERGE_CONFIRM=0 SAFE_MERGE_MAX_WAIT=5

HEAD=abcdef1234567890abcdef1234567890abcdef12
MERGE_SHA=9876543210fedcba9876543210fedcba98765432

# Stub gh: answers from files in $STUB_DIR, logs each call to $STUB_DIR/calls.
cat >"${WORK}/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >>"$STUB_DIR/calls"
case "$1 $2" in
  "pr view")
    if [[ "$*" == *"statusCheckRollup"* ]]; then cat "$STUB_DIR/view.json"; exit 0; fi
    if [[ "$*" == *"--json title"* && -f "$STUB_DIR/title-fail" ]]; then exit 1; fi
    jqexpr=""; prev=""
    for a in "$@"; do [[ "$prev" == "--jq" ]] && jqexpr="$a"; prev="$a"; done
    jq -r "$jqexpr" "$STUB_DIR/after-merge.json" ;;
  "api graphql")
    # Merge queue reads: serve queue.1, queue.2, ... in order; the last one
    # sticks. A file holding FAIL is a failed read (API error).
    jqexpr=""; prev=""
    for a in "$@"; do [[ "$prev" == "--jq" ]] && jqexpr="$a"; prev="$a"; done
    mapfile -t qs < <(ls "$STUB_DIR"/queue.* 2>/dev/null | sort -V)
    [[ ${#qs[@]} -gt 0 ]] || exit 1
    rc=0
    if [[ "$(cat "${qs[0]}")" == FAIL ]]; then rc=1; else jq -r "$jqexpr" "${qs[0]}"; fi
    [[ ${#qs[@]} -gt 1 ]] && rm -f "${qs[0]}"
    exit "$rc" ;;
  "api -X")
    # #1809: the "Reviewed head" status POST; status-fail makes it fail.
    [[ -f "$STUB_DIR/status-fail" ]] && exit 1; exit 0 ;;
  "api repos/{owner}/{repo}/rules/branches/main")
    # #1809 ruleset guard: rules.json (missing = API error).
    jqexpr=""; prev=""
    for a in "$@"; do [[ "$prev" == "--jq" ]] && jqexpr="$a"; prev="$a"; done
    [[ -f "$STUB_DIR/rules.json" ]] || exit 1
    jq -r "$jqexpr" "$STUB_DIR/rules.json" ;;
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
  jq '. + {title: "fix: the title"}' "${STUB_DIR}/after-merge.json" >"${STUB_DIR}/am.tmp" && mv "${STUB_DIR}/am.tmp" "${STUB_DIR}/after-merge.json"
}
queue_fx() {  # n state in_queue entry_state merge_sha [auto_merge_enabled_at]
  printf '{"data":{"repository":{"pullRequest":{"state":"%s","headRefOid":"%s","isInMergeQueue":%s,"url":"https://github.com/o/r/pull/42","mergeQueueEntry":%s,"mergeCommit":%s,"autoMergeRequest":%s}}}}' \
    "$2" "$HEAD" "$3" "$( [[ -n "$4" ]] && printf '{"state":"%s"}' "$4" || echo null)" \
    "$( [[ -n "$5" ]] && printf '{"oid":"%s"}' "$5" || echo null)" \
    "$( [[ -n "${6-}" ]] && printf '{"enabledAt":"%s"}' "$6" || echo null)" >"${STUB_DIR}/queue.$1"
}
queue_fail() { echo FAIL >"${STUB_DIR}/queue.$1"; }  # n: this read fails
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
run 42 abcdef1 --comment-file "${WORK}/comment.md"
[[ $RC -eq 2 ]] || fail "--comment-file without --close: exit $RC"
# #2036: arguments are validated before any gh call (a wait would come first).
run 42 abcdef1 --close 7 --note-issue 9
[[ $RC -eq 2 && -z "$(calls)" ]] || fail "--note-issue without --note-file: rc=$RC calls=$(calls)"
run 42 abcdef1 --close 7 --note-file "${WORK}/note.md"
[[ $RC -eq 2 && -z "$(calls)" ]] || fail "--note-file without --note-issue: rc=$RC calls=$(calls)"
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

# 10. #1523: GitHub takes required checks from the NEWEST run of a workflow, so a
# newer all-SKIPPED run must not be hidden behind an older green one.
# (a) newest run 300 is all SKIPPED incl. CI Complete: not green, never merges.
reset
view "$(check 'Integration Tests' COMPLETED SUCCESS 200)" "$(check 'CI Complete' COMPLETED SUCCESS 200)" \
  "$(check 'Lint' COMPLETED SUCCESS 200)" \
  "$(check 'Integration Tests' COMPLETED SKIPPED 300)" "$(check 'CI Complete' COMPLETED SKIPPED 300)" \
  "$(check 'Lint' COMPLETED NEUTRAL 300)"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 1 && "$OUT" == *"CI Complete is SKIPPED"* ]] || fail "10a newest skipped run: rc=$RC $OUT"
calls | grep -qE 'pr merge|issue' && fail "10a newest skipped run must not merge: $(calls)"
# (b) newest run 300 has no "CI Complete" (old label-event run): waits, times out.
reset
view "$(check 'Integration Tests' COMPLETED SUCCESS 200)" "$(check 'CI Complete' COMPLETED SUCCESS 200)" \
  "$(check 'Lint' COMPLETED SUCCESS 200)" \
  "$(check 'Label event (no CI result)' COMPLETED SKIPPED 300)" "$(check 'Lint' COMPLETED SKIPPED 300)"
run 42 abcdef1
[[ $RC -eq 4 ]] || fail "10b no CI Complete in newest run: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "10b must not merge: $(calls)"
# (c) other workflows group by name: a newer skipped "ci-full label" run is fine
# while CI is green; a FAILURE as its newest run blocks; IN_PROGRESS merges (accepted gap).
FULL='Re-run CI in full mode'
for variant in success inprogress; do
  reset
  case "$variant" in
    success) full="$(check "$FULL" COMPLETED SUCCESS 250 'ci-full label')" ;;
    inprogress) full="$(check "$FULL" IN_PROGRESS '' 250 'ci-full label')" ;;
  esac
  view "$GREEN" "$full" "$(check "$FULL" COMPLETED SKIPPED 260 'ci-full label')"
  run 42 abcdef1
  # The inprogress variant pins the accepted gap (an IN_PROGRESS run of another workflow
  # does not block the merge). Flip its expectation if that gap is closed.
  [[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"* ]] || fail "10c $variant: rc=$RC $OUT"
done
reset
view "$GREEN" "$(check "$FULL" COMPLETED FAILURE 250 'ci-full label')"
run 42 abcdef1
[[ $RC -eq 1 && "$OUT" == *"CI RED"* ]] || fail "10c failure as newest: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "10c failure must not merge: $(calls)"
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

# 15. CONFLICTING: fail fast with exit 5 (GitHub runs no CI on it), no merge.
reset; SAFE_MERGE_MAX_WAIT=8
printf '{"headRefOid":"%s","state":"OPEN","mergeable":"CONFLICTING","statusCheckRollup":[%s]}' "$HEAD" \
  "$(check 'CI Complete' QUEUED '' 100)" >"${STUB_DIR}/view.json"
run 42 abcdef1 --rerun-failed-once --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 5 && "$OUT" == *"CONFLICTING"*"rebase"* ]] || fail "conflicting: rc=$RC $OUT"
calls | grep -qE 'pr merge|issue|run rerun' && fail "conflicting must not merge, rerun or touch issues: $(calls)"
run 42 abcdef1 --wait-ci
[[ $RC -eq 5 ]] || fail "conflicting --wait-ci: rc=$RC $OUT"
# MERGEABLE / UNKNOWN never trip it.
reset; printf '{"headRefOid":"%s","state":"OPEN","mergeable":"UNKNOWN","statusCheckRollup":[%s]}' "$HEAD" "$GREEN" \
  >"${STUB_DIR}/view.json"
run 42 abcdef1
[[ $RC -eq 0 && "$OUT" == *"MERGED as"* ]] || fail "mergeable UNKNOWN: rc=$RC $OUT"
SAFE_MERGE_MAX_WAIT=5
ok

# 16. Newest run cancelled before its matrix expanded (runs 39206900261 /
#     39213800861): infrastructure; --rerun-failed-once reruns the WHOLE run.
CI_COMPLETE_EXPR="(github.event.action == 'labeled' && github.event.label.name != 'ci-full') && 'Label event (no CI result)' || 'CI Complete'"
CANCELLED_RUN="$(check 'Detect changes' COMPLETED CANCELLED 400),$(check 'Build ${{ matrix.service }}' COMPLETED CANCELLED 400),$(check 'Publish ${{ matrix.service }}' COMPLETED CANCELLED 400),$(check 'Retag ${{ matrix.image }}' COMPLETED CANCELLED 400),$(check 'Integration Tests' COMPLETED CANCELLED 400),$(check 'Lint Dockerfiles' COMPLETED SKIPPED 400),$(check "${CI_COMPLETE_EXPR}" COMPLETED CANCELLED 400)"
# Template-named jobs that every real run carries as SKIPPED.
SKIPPED_TEMPLATES="$(check 'Publish ${{ matrix.service }}' COMPLETED SKIPPED 100),$(check 'Retag ${{ matrix.image }}' COMPLETED SKIPPED 100)"
reset; view "$CANCELLED_RUN"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 1 && "$OUT" == *"infra=cancelled-run"*"gh run rerun 400"* ]] || fail "cancelled run: rc=$RC $OUT"
calls | grep -qE 'pr merge|issue|run rerun' && fail "cancelled run without --rerun-failed-once must only report: $(calls)"
reset; view "$CANCELLED_RUN"
printf '{"headRefOid":"%s","state":"OPEN","statusCheckRollup":[%s,%s]}' "$HEAD" \
  "$(check 'Integration Tests' COMPLETED SUCCESS 400)" "$(check 'CI Complete' COMPLETED SUCCESS 400)" \
  >"${STUB_DIR}/view-after-rerun.json"
run 42 abcdef1 --rerun-failed-once
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"*"after one rerun"* ]] || fail "cancelled rerun: rc=$RC $OUT"
calls | grep -qx 'gh run rerun 400' || fail "cancelled run must be rerun whole (no --failed): $(calls)"
# Superseded by a newer green run on the same head: ignored, no rerun.
reset; view "$CANCELLED_RUN" "$(check 'Lint' COMPLETED SUCCESS 500)" "$(check 'CI Complete' COMPLETED SUCCESS 500)"
run 42 abcdef1 --rerun-failed-once
[[ $RC -eq 0 && "$OUT" == *"MERGED as"* && "$OUT" != *"rerun"* ]] || fail "superseded cancelled run: rc=$RC $OUT"
calls | grep -q 'run rerun' && fail "superseded cancelled run must not be rerun: $(calls)"
# Still cancelled after the one rerun: rc 1 infra=cancelled-run, exactly one rerun.
reset; view "$CANCELLED_RUN"
run 42 abcdef1 --rerun-failed-once --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 1 && "$OUT" == *"infra=cancelled-run"* ]] || fail "still cancelled after rerun: rc=$RC $OUT"
[[ "$(grep -c 'run rerun' "${STUB_DIR}/calls")" -eq 1 ]] || fail "cancelled run must be rerun exactly once: $(calls)"
calls | grep -qE 'pr merge|issue' && fail "still cancelled must not merge or touch issues: $(calls)"
# Job timeout (expanded job CANCELLED, SKIPPED template jobs, CI Complete FAILURE): not infra.
reset; view "$(check 'Lint' COMPLETED SUCCESS 100)" "$(check 'Integration Tests' COMPLETED CANCELLED 100)" \
  "$SKIPPED_TEMPLATES" "$(check 'CI Complete' COMPLETED FAILURE 100)"
run 42 abcdef1
[[ $RC -eq 1 && "$OUT" == *"not infrastructure"* ]] || fail "timeout-cancel: rc=$RC $OUT"
# Human cancel mid-run (matrix expanded, expanded jobs CANCELLED, SKIPPED
# template jobs, CI Complete SKIPPED): not infra, never rerun.
reset; view "$(check 'Lint' COMPLETED SUCCESS 100)" "$(check 'Integration Tests' COMPLETED CANCELLED 100)" \
  "$(check 'Build menu' COMPLETED CANCELLED 100)" "$SKIPPED_TEMPLATES" "$(check 'CI Complete' COMPLETED SKIPPED 100)"
run 42 abcdef1 --rerun-failed-once
[[ $RC -eq 1 && "$OUT" != *"cancelled-run"* ]] || fail "human cancel mid-run: rc=$RC $OUT"
calls | grep -qE 'run rerun|pr merge' && fail "human cancel must not be rerun or merged: $(calls)"
reset; view "$CANCELLED_RUN" "$(check 'Lib Unit Tests' COMPLETED FAILURE 400)"
run 42 abcdef1
[[ $RC -eq 1 && "$OUT" != *"cancelled-run"* ]] || fail "cancel next to a failure: rc=$RC $OUT"
ok

# 17. Merge queue (#1555): `gh pr merge` enqueues instead of merging.
# Without --wait-queue: QUEUED, exit 6, issues untouched.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN true AWAITING_CHECKS ""
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 6 && "$OUT" == "#42 QUEUED in the merge queue (head abcdef12, entry AWAITING_CHECKS)"*"no issue changes"* ]] \
  || fail "queued: rc=$RC $OUT"
calls | grep -q 'issue' && fail "queued must not touch issues: $(calls)"
calls | grep -q 'pr merge 42 --squash --match-head-commit '"$HEAD" || fail "enqueue at full head: $(calls)"
# --wait-queue: the queue merges it -> normal MERGED path, issues closed.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN true AWAITING_CHECKS ""; queue_fx 2 OPEN true MERGEABLE ""; queue_fx 3 MERGED false "" "$MERGE_SHA"
run 42 abcdef1 --wait-queue --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 0 && "$OUT" == "#42 MERGED as 98765432 (head abcdef12); closed #7" ]] || fail "queue merged: rc=$RC $OUT"
calls | grep -q 'issue close 7 --comment Merged in 98765432, thanks.' || fail "queue merged close: $(calls)"
# --wait-queue: ejected (left the queue, still OPEN on the confirming read) -> exit 7.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN true AWAITING_CHECKS ""; queue_fx 2 OPEN false "" ""; queue_fx 3 OPEN false "" ""
run 42 abcdef1 --wait-queue --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 7 && "$OUT" == *"EJECTED"*"no issue changes"* ]] || fail "ejected: rc=$RC $OUT"
calls | grep -q 'issue' && fail "ejected must not touch issues: $(calls)"
# Left the queue a moment before reading MERGED: the confirming read wins.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN true MERGEABLE ""; queue_fx 2 OPEN false "" ""; queue_fx 3 MERGED false "" "$MERGE_SHA"
run 42 abcdef1 --wait-queue
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"* ]] || fail "dequeue-then-merged race: rc=$RC $OUT"
# Closed while queued: ejected.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN true QUEUED ""; queue_fx 2 CLOSED false "" ""
run 42 abcdef1 --wait-queue
[[ $RC -eq 7 && "$OUT" == *"EJECTED: PR is CLOSED"* ]] || fail "closed while queued: rc=$RC $OUT"
# Wait budget spent while still queued: QUEUED, exit 6.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""; queue_fx 1 OPEN true QUEUED ""
SAFE_MERGE_QUEUE_WAIT=0 run 42 abcdef1 --wait-queue --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 6 && "$OUT" == *"QUEUED after 0s wait"* ]] || fail "queue wait spent: rc=$RC $OUT"
calls | grep -q 'issue' && fail "still queued must not touch issues: $(calls)"
# Not merged and not in the queue: still MERGE FAILED (exit 1).
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""; queue_fx 1 OPEN false "" ""
run 42 abcdef1 --wait-queue
[[ $RC -eq 1 && "$OUT" == *"MERGE FAILED"* ]] || fail "not queued, not merged: rc=$RC $OUT"
# gh pr view still reads OPEN but GraphQL already reads MERGED: merged, issues closed.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""; queue_fx 1 MERGED false "" "$MERGE_SHA"
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 0 && "$OUT" == "#42 MERGED as 98765432 (head abcdef12); closed #7" ]] || fail "view OPEN, graphql MERGED: rc=$RC $OUT"
# Auto-merge enabled instead of enqueued (checks still pending): QUEUED, exit 6.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN false "" "" 2026-10-19T10:00:00Z
run 42 abcdef1 --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 6 && "$OUT" == *"QUEUED"*"auto-merge enabled at 2026-10-19T10:00:00Z"*"no issue changes"* ]] \
  || fail "auto-merge armed: rc=$RC $OUT"
calls | grep -q 'issue' && fail "auto-merge armed must not touch issues: $(calls)"
# --wait-queue with auto-merge armed: waits through auto-merge -> queue -> MERGED.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN false "" "" 2026-10-19T10:00:00Z; queue_fx 2 OPEN false "" "" 2026-10-19T10:00:00Z
queue_fx 3 OPEN true AWAITING_CHECKS ""; queue_fx 4 MERGED false "" "$MERGE_SHA"
run 42 abcdef1 --wait-queue --close 7 --comment-file "${WORK}/comment.md"
[[ $RC -eq 0 && "$OUT" == "#42 MERGED as 98765432 (head abcdef12); closed #7" ]] || fail "auto-merge then merged: rc=$RC $OUT"
# A failed confirming read never counts as "not in the queue": dequeued, read
# fails, still dequeued, then MERGED (beyond a single settle) -> merged.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN true MERGEABLE ""; queue_fx 2 OPEN false "" ""; queue_fail 3; queue_fx 4 OPEN false "" ""
queue_fx 5 MERGED false "" "$MERGE_SHA"
run 42 abcdef1 --wait-queue
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"* ]] || fail "failed confirm read: rc=$RC $OUT"
# Every confirming read fails: keep waiting, never EJECTED; then MERGED.
reset; view "$GREEN"; after_merge OPEN "$HEAD" ""
queue_fx 1 OPEN true MERGEABLE ""; queue_fx 2 OPEN false "" ""; queue_fail 3; queue_fail 4; queue_fail 5
queue_fx 6 MERGED false "" "$MERGE_SHA"
run 42 abcdef1 --wait-queue
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"* ]] || fail "all confirm reads failed: rc=$RC $OUT"
# --wait-ci never enqueues.
reset; view "$GREEN"; queue_fx 1 OPEN true QUEUED ""
run 42 abcdef1 --wait-ci --wait-queue
[[ $RC -eq 0 && "$OUT" == *"CI GREEN"* ]] || fail "--wait-ci with --wait-queue: rc=$RC $OUT"
calls | grep -qE 'pr merge|api graphql' && fail "--wait-ci must not enqueue: $(calls)"
ok

# #2036: --close without --comment-file uses a default comment, no usage error.
reset; view "$GREEN"
run 42 abcdef1 --close 7,8
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"* && "$OUT" == *"closed #7"* ]] || fail "default close comment: rc=$RC $OUT"
calls | grep -qF 'issue close 7 --comment Done in #42: fix: the title' || fail "default comment text: $(calls)"
calls | grep -qF 'issue close 8 --comment Done in #42: fix: the title' || fail "default comment text for 8: $(calls)"
ok

# The title read fails: the default comment is exactly "Done in #<PR>", no ": ".
reset; view "$GREEN"; touch "${STUB_DIR}/title-fail"
run 42 abcdef1 --close 7
[[ $RC -eq 0 && "$OUT" == *"closed #7"* ]] || fail "title read fails: rc=$RC $OUT"
calls | grep -qxF 'gh issue close 7 --comment Done in #42' || fail "comment without title must be exactly 'Done in #42': $(calls)"
ok

# 18. #1809 --arm: ruleset guard, then the status POST, then auto-merge; exit 8, no waiting.
rules() {  # contexts... -> rules.json: required_status_checks + merge_queue (RULES_GROUPING, default ALLGREEN)
  local ctx="" c
  for c in "$@"; do ctx+="${ctx:+,}{\"context\":\"$c\"}"; done
  printf '[{"type":"deletion"},{"type":"required_status_checks","parameters":{"required_status_checks":[%s]}},{"type":"merge_queue","parameters":{"grouping_strategy":"%s"}}]' \
    "$ctx" "${RULES_GROUPING:-ALLGREEN}" >"${STUB_DIR}/rules.json"
}
REVIEW_URL=https://github.com/o/r/pull/42#issuecomment-1
# Happy path: POST before the merge, full SHA, ARMED, no issue calls.
reset; view "$GREEN"; rules "CI Complete" "Reviewed head"
queue_fx 1 OPEN false "" "" 2026-10-31T10:00:00Z
run 42 "$HEAD" --arm --review-url "$REVIEW_URL" --depth full
[[ $RC -eq 8 && "$OUT" == "#42 ARMED (head abcdef12, auto-merge: auto-merge enabled at 2026-10-31T10:00:00Z)" ]] \
  || fail "arm: rc=$RC $OUT ($(cat "${WORK}/stderr"))"
post_line="$(grep -n "api -X POST repos/{owner}/{repo}/statuses/$HEAD -f state=success -f context=Reviewed head -f description=review-pr full abcdef12 -f target_url=$REVIEW_URL" "${STUB_DIR}/calls" | cut -d: -f1 || true)"
merge_line="$(grep -n "pr merge 42 --auto --squash --match-head-commit $HEAD" "${STUB_DIR}/calls" | cut -d: -f1 || true)"
[[ -n "$post_line" && -n "$merge_line" ]] || fail "arm calls (status POST and auto-merge): $(calls)"
(( post_line < merge_line )) || fail "status must be posted before arming: $(calls)"
calls | grep -q '^gh issue' && fail "--arm must not touch issues: $(calls)"
# In the queue already: ARMED (queued).
reset; view "$GREEN"; rules "Reviewed head"; queue_fx 1 OPEN true AWAITING_CHECKS ""
run 42 "$HEAD" --arm
[[ $RC -eq 8 && "$OUT" == *"ARMED (head abcdef12, queued: entry AWAITING_CHECKS)"* ]] || fail "arm queued: rc=$RC $OUT"
# CI red or pending: still arms (GitHub queues once the required checks pass).
for ci in "$(check 'Integration Tests' COMPLETED FAILURE 300)" "$(check 'CI Complete' IN_PROGRESS '' 300)"; do
  reset; view "$ci"; rules "Reviewed head"; queue_fx 1 OPEN false "" "" 2026-10-31T10:00:00Z
  run 42 "$HEAD" --arm
  [[ $RC -eq 8 && "$OUT" == *ARMED* ]] || fail "arm with CI not green: rc=$RC $OUT"
  calls | grep -q 'pr merge 42 --auto' || fail "arm with CI not green must arm: $(calls)"
done
# The state read after arming fails: ARMED (state unverified).
reset; view "$GREEN"; rules "Reviewed head"; queue_fail 1
run 42 "$HEAD" --arm
[[ $RC -eq 8 && "$OUT" == *"ARMED (head abcdef12, state unverified)"* ]] || fail "arm unverified: rc=$RC $OUT"
# gh pr merge --auto fails and the PR is not armed: ARM FAILED, exit 1.
reset; view "$GREEN"; rules "Reviewed head"; echo 1 >"${STUB_DIR}/merge-rc"; queue_fx 1 OPEN false "" ""
run 42 "$HEAD" --arm
[[ $RC -eq 1 && "$OUT" == *"ARM FAILED"* ]] || fail "arm failed: rc=$RC $OUT"
# The status POST fails: no arm.
reset; view "$GREEN"; rules "Reviewed head"; touch "${STUB_DIR}/status-fail"
run 42 "$HEAD" --arm
[[ $RC -eq 1 && "$OUT" == *"NOT ARMED"*"could not post"* ]] || fail "status post fails: rc=$RC $OUT"
calls | grep -q 'pr merge' && fail "failed status post must not arm: $(calls)"
# Moved head: exit 3, nothing posted, no merge.
reset; view "$GREEN"; rules "Reviewed head"
run 42 1111111111111111111111111111111111111111 --arm
[[ $RC -eq 3 && "$OUT" == *"does not match 11111111"* ]] || fail "arm moved head: rc=$RC $OUT"
calls | grep -qE 'api -X|pr merge' && fail "moved head must not post or arm: $(calls)"
# CONFLICTING: exit 5, nothing posted.
reset; rules "Reviewed head"
printf '{"headRefOid":"%s","state":"OPEN","mergeable":"CONFLICTING","statusCheckRollup":[%s]}' "$HEAD" "$GREEN" \
  >"${STUB_DIR}/view.json"
run 42 "$HEAD" --arm
[[ $RC -eq 5 && "$OUT" == *"CONFLICTING"* ]] || fail "arm conflicting: rc=$RC $OUT"
calls | grep -qE 'api -X|pr merge' && fail "conflicting must not post or arm: $(calls)"
# Ruleset lacks the context, or the rules read fails: exit 2 "not enforced", nothing posted.
# Queue grouping other than ALLGREEN: refuse too (fail closed).
reset; view "$GREEN"; RULES_GROUPING=HEADGREEN rules "Reviewed head"
run 42 "$HEAD" --arm
[[ $RC -eq 2 && "$OUT" == *"not enforced"* ]] || fail "HEADGREEN grouping: rc=$RC $OUT"
calls | grep -qE 'api -X|pr merge|pr view' && fail "HEADGREEN must not post or arm: $(calls)"
reset; view "$GREEN"; rules "CI Complete" "Reviewed head (old)"
run 42 "$HEAD" --arm
[[ $RC -eq 2 && "$OUT" == *"not enforced"* ]] || fail "ruleset without context: rc=$RC $OUT"
calls | grep -qE 'api -X|pr merge|pr view' && fail "unenforced ruleset must not read, post or arm: $(calls)"
reset; view "$GREEN"
run 42 "$HEAD" --arm
[[ $RC -eq 2 && "$OUT" == *"not enforced"* ]] || fail "rules read fails: rc=$RC $OUT"
calls | grep -qE 'api -X|pr merge' && fail "failed rules read must not post or arm: $(calls)"
# Usage: short prefix, combined with waiting / issue options, --arm with --revoke: exit 2, no gh call.
reset; view "$GREEN"; rules "Reviewed head"
for args in "abcdef1 --arm" "abcdef12 --revoke" "$HEAD --arm --close 7" "$HEAD --arm --wait-ci" \
  "$HEAD --arm --wait-queue" "$HEAD --arm --rerun-failed-once" "$HEAD --arm --revoke" \
  "$HEAD --arm --note-issue 9 --note-file ${WORK}/note.md" "$HEAD --depth full" \
  "$HEAD --review-url $REVIEW_URL" "$HEAD --arm --review-url http://x" "$HEAD --arm --depth Full;x"; do
  read -r -a argv <<<"$args"
  run 42 "${argv[@]}"
  [[ $RC -eq 2 ]] || fail "usage '$args': exit $RC ($OUT)"
done
[[ -z "$(calls)" ]] || fail "--arm usage errors must not call gh: $(calls)"
ok

# 19. #1809 --revoke posts a failure status on the full SHA; no merge, no PR read.
reset
run 42 "$HEAD" --revoke --review-url "$REVIEW_URL"
[[ $RC -eq 0 && "$OUT" == *"REVOKED review of abcdef12"* ]] || fail "revoke: rc=$RC $OUT"
calls | grep -qF "api -X POST repos/{owner}/{repo}/statuses/$HEAD -f state=failure -f context=Reviewed head" \
  || fail "revoke must post state=failure: $(calls)"
calls | grep -qE 'pr merge|pr view|^gh issue' && fail "revoke must only post the status: $(calls)"
reset; touch "${STUB_DIR}/status-fail"
run 42 "$HEAD" --revoke
[[ $RC -eq 1 && "$OUT" == *"REVOKE FAILED"* ]] || fail "revoke post fails: rc=$RC $OUT"
ok

# 20. #1809: the plain merge path posts the "Reviewed head" status before merging,
# and a failed POST does not block the merge.
reset; view "$GREEN"
run 42 abcdef1
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"* ]] || fail "merge with status: rc=$RC $OUT"
post_line="$(grep -n "api -X POST repos/{owner}/{repo}/statuses/$HEAD -f state=success -f context=Reviewed head" "${STUB_DIR}/calls" | cut -d: -f1 || true)"
merge_line="$(grep -n "pr merge 42 --squash --match-head-commit $HEAD" "${STUB_DIR}/calls" | cut -d: -f1 || true)"
[[ -n "$post_line" && -n "$merge_line" ]] && (( post_line < merge_line )) || fail "status before merge: $(calls)"
# ... and before the CI wait: with CI pending the status is posted already.
reset; SAFE_MERGE_MAX_WAIT=0
view "$(check 'Lint' COMPLETED SUCCESS 100)" "$(check 'CI Complete' QUEUED '' 100)"
run 42 abcdef1
[[ $RC -eq 4 ]] || fail "pending CI times out: rc=$RC $OUT"
SAFE_MERGE_MAX_WAIT=5
calls | grep -q "api -X POST repos/{owner}/{repo}/statuses/$HEAD -f state=success -f context=Reviewed head" \
  || fail "status must be posted before the CI wait (rc=$RC $OUT): $(calls)"
reset; view "$GREEN"; touch "${STUB_DIR}/status-fail"
run 42 abcdef1
[[ $RC -eq 0 && "$OUT" == *"MERGED as 98765432"* ]] || fail "failed status post must not block the merge: rc=$RC $OUT"
# --wait-ci posts nothing.
reset; view "$GREEN"
run 42 abcdef1 --wait-ci
calls | grep -q 'api -X' && fail "--wait-ci must not post a status: $(calls)"
ok

echo "test-safe-merge: ${pass} groups passed"
