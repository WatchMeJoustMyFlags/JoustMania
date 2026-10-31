#!/usr/bin/env bash
# Self-contained tests for scripts/ci/merge-sweep.sh (#1809).
#
# A stub `gh` on PATH serves the PR list, statuses, comments and issue fixtures
# and records every call; a stub safe-merge records --arm calls. No network or
# real PR is touched.
#
#   ./scripts/tests/test-merge-sweep.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SWEEP="${REPO_ROOT}/scripts/ci/merge-sweep.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export STUB_DIR="${WORK}/stub"
mkdir -p "${STUB_DIR}" "${WORK}/bin"

H1=1111111111111111111111111111111111111111
H2=2222222222222222222222222222222222222222
H3=3333333333333333333333333333333333333333

# Stub gh. Fixtures in $STUB_DIR:
#   prs.json                   answer to the PR-list query (GraphQL)
#   statuses-<sha>.json        commit statuses of a head (missing: [])
#   comments-<n>.json          issue/PR comments (missing: [])
#   issue-<n>.state            issue state (missing: OPEN)
#   fail-<key>                 make that call fail: prs, dequeue, statuses-<sha>, comments-<n>, comment
cat >"${WORK}/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >>"$STUB_DIR/calls"
jqexpr=""; prev=""; query=""
for a in "$@"; do
  [[ "$prev" == "--jq" ]] && jqexpr="$a"
  [[ "$prev" == "-f" && "$a" == query=* ]] && query="$a"
  prev="$a"
done
case "$1 $2" in
  "api graphql")
    if [[ "$query" == *dequeuePullRequest* ]]; then
      [[ -f "$STUB_DIR/fail-dequeue" ]] && exit 1
      echo '{"data":{"dequeuePullRequest":{"clientMutationId":null}}}'
    else
      [[ -f "$STUB_DIR/fail-prs" ]] && exit 1
      cat "$STUB_DIR/prs.json"
    fi ;;
  "api repos/{owner}/{repo}/commits/"*)
    sha="${2#repos/\{owner\}/\{repo\}/commits/}"; sha="${sha%%/*}"
    [[ -f "$STUB_DIR/fail-statuses-$sha" ]] && exit 1
    f="$STUB_DIR/statuses-$sha.json"; [[ -f "$f" ]] || f=/dev/null
    { cat "$f"; [[ -s "$f" ]] || echo '[]'; } | jq -r "$jqexpr" ;;
  "api repos/{owner}/{repo}/issues/"*)
    n="${2#repos/\{owner\}/\{repo\}/issues/}"; n="${n%%/*}"
    [[ -f "$STUB_DIR/fail-comments-$n" ]] && exit 1
    f="$STUB_DIR/comments-$n.json"; [[ -f "$f" ]] || f=/dev/null
    { cat "$f"; [[ -s "$f" ]] || echo '[]'; } | jq -r "$jqexpr" ;;
  "pr comment")
    [[ -f "$STUB_DIR/fail-comment" ]] && exit 1
    exit 0 ;;
  "issue view")
    f="$STUB_DIR/issue-$3.state"; if [[ -f "$f" ]]; then cat "$f"; else echo OPEN; fi ;;
  "issue close"|"issue comment") exit 0 ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
STUB
chmod +x "${WORK}/bin/gh"
# Stub safe-merge: records its arguments, answers ARMED (exit 8) or $STUB_DIR/sm-rc.
cat >"${WORK}/bin/safe-merge" <<'STUB'
#!/usr/bin/env bash
echo "safe-merge $*" >>"$STUB_DIR/calls"
rc=8; [[ -f "$STUB_DIR/sm-rc" ]] && rc="$(cat "$STUB_DIR/sm-rc")"
echo "#$1 ARMED (head ${2:0:8}, queued: entry QUEUED)"
exit "$rc"
STUB
chmod +x "${WORK}/bin/safe-merge"
export PATH="${WORK}/bin:${PATH}"
export SWEEP_SAFE_MERGE="${WORK}/bin/safe-merge"

pass=0
fail() { echo "FAIL: $*" >&2; echo "--- out:" >&2; echo "$OUT" >&2; echo "--- calls:" >&2; calls >&2; exit 1; }
ok() { pass=$((pass + 1)); }
calls() { cat "${STUB_DIR}/calls" 2>/dev/null || true; }

# open PR node: number author head [queue-state] [auto yes|no] [removal reason] [mergeable]
opr() {
  local q="null" auto="null" tl="[]"
  [[ -n "${4:-}" ]] && q="{\"state\":\"$4\"}"
  [[ "${5:-no}" == yes ]] && auto='{"enabledAt":"2026-10-31T20:00:00Z"}'
  [[ -n "${6:-}" ]] && tl="[{\"reason\":\"$6\"}]"
  printf '{"number":%s,"id":"PR_%s","author":{"login":"%s"},"headRefOid":"%s","mergeable":"%s","autoMergeRequest":%s,"mergeQueueEntry":%s,"timelineItems":{"nodes":%s}}' \
    "$1" "$1" "$2" "$3" "${7:-MERGEABLE}" "$auto" "$q" "$tl"
}
# merged PR node: number author body(json string) [mergedAt]
mpr() {
  printf '{"number":%s,"author":{"login":"%s"},"headRefName":"feat/x-%s","mergedAt":"%s","mergeCommit":{"oid":"abcdef0123456789abcdef0123456789abcdef01"},"body":%s}' \
    "$1" "$2" "$1" "${4:-$(date -u +%FT%TZ)}" "$3"
}
prs() {  # open-json merged-json (comma-joined node lists)
  printf '{"data":{"repository":{"open":{"nodes":[%s]},"merged":{"nodes":[%s]}}}}' "$1" "${2:-}" >"${STUB_DIR}/prs.json"
}
st() { printf '{"id":%s,"state":"%s","context":"%s","creator":{"login":"%s"}}' "$1" "$2" "${4:-Reviewed head}" "$3"; }
statuses() { local sha="$1"; shift; local IFS=,; printf '[%s]' "$*" >"${STUB_DIR}/statuses-$sha.json"; }
reset() { rm -f "${STUB_DIR}"/*; }
run() {  # [args] -> OUT, RC
  set +e
  OUT="$(env -u SWEEP_AUTHORS -u REVIEW_APPROVERS timeout 30 "${SWEEP}" "$@" 2>"${WORK}/stderr")"
  RC=$?
  set -e
}

# 1. UNMERGEABLE entry of ours: dequeued, then re-armed once (newest status success by aepfli).
reset; prs "$(opr 42 aepfli $H1 UNMERGEABLE no failed_checks)"
statuses $H1 "$(st 900 success aepfli)"
run
[[ $RC -eq 0 ]] || fail "unmergeable: rc=$RC"
calls | grep -q 'api graphql -f query=mutation.*dequeuePullRequest.* -f id=PR_42' || fail "no dequeue of PR_42"
calls | grep -qF "pr comment 42 --body" || fail "no once-per-head marker"
calls | grep -qF "<!-- merge-sweep rearm $H1 -->" || fail "marker text"
calls | grep -qxF "safe-merge 42 $H1 --arm --depth rearm" || fail "no re-arm"
# The marker is posted before arming.
[[ "$(calls | grep -n 'pr comment 42' | cut -d: -f1)" -lt "$(calls | grep -n '^safe-merge 42' | cut -d: -f1)" ]] || fail "marker after arm"
[[ "$OUT" == *"#42 RE-ARMED: dequeued by this sweep (UNMERGEABLE)"* ]] || fail "unmergeable line"
ok

# 1b. dequeue fails: reported, exit 1, no re-arm.
reset; prs "$(opr 42 aepfli $H1 UNMERGEABLE)"; statuses $H1 "$(st 900 success aepfli)"; touch "${STUB_DIR}/fail-dequeue"
run
[[ $RC -eq 1 && "$OUT" == *"#42 DEQUEUE FAILED"* ]] || fail "dequeue failure: rc=$RC"
calls | grep -q '^safe-merge' && fail "re-armed after a failed dequeue"
ok

# 2. Ejected (failed_checks), success by approver: re-armed once ...
reset; prs "$(opr 43 aepfli $H2 '' no failed_checks)"; statuses $H2 "$(st 5 success aepfli)"
run
[[ $RC -eq 0 && "$OUT" == *"#43 RE-ARMED: removed from the queue (failed_checks)"* ]] || fail "ejected re-arm: rc=$RC"
calls | grep -qxF "safe-merge 43 $H2 --arm --depth rearm" || fail "no re-arm of 43"
# ... and not a second time when the marker for this head exists.
printf '[{"body":"Merge sweep: re-armed\\n<!-- merge-sweep rearm %s -->"}]' "$H2" >"${STUB_DIR}/comments-43.json"
rm -f "${STUB_DIR}/calls"
run
[[ "$OUT" == *"#43 EJECTED: removed from the queue (failed_checks) again; already re-armed once"*"next: fix the failing test"* ]] || fail "second re-arm line"
calls | grep -q '^safe-merge' && fail "re-armed twice at one head"
calls | grep -q 'pr comment' && fail "second marker posted"
# A marker for an older head does not block the new head.
printf '[{"body":"<!-- merge-sweep rearm %s -->"}]' "$H1" >"${STUB_DIR}/comments-43.json"
rm -f "${STUB_DIR}/calls"
run
calls | grep -qxF "safe-merge 43 $H2 --arm --depth rearm" || fail "old-head marker blocked the new head"
ok

# 3. No re-arm when the newest status is failure (revoke), even with an older success.
reset; prs "$(opr 44 aepfli $H3 '' no failed_checks)"
statuses $H3 "$(st 901 failure aepfli)" "$(st 900 success aepfli)"
run
[[ "$OUT" == *"#44 EJECTED"*"newest \"Reviewed head\" on 33333333 is failure"* ]] || fail "revoked line"
calls | grep -qE '^safe-merge|pr comment' && fail "revoked PR re-armed"
# Highest id wins whatever the listing order.
statuses $H3 "$(st 900 success aepfli)" "$(st 901 failure aepfli)"
rm -f "${STUB_DIR}/calls"; run
calls | grep -q '^safe-merge' && fail "revoke listed second still re-armed"
ok

# 4. No re-arm when the newest success is by a non-approver or a bot, or there is no status.
reset; prs "$(opr 45 aepfli $H1 '' no failed_checks)"; statuses $H1 "$(st 7 success mallory)"
run
[[ "$OUT" == *"#45 EJECTED"*"by mallory, not an approver"* ]] || fail "non-approver line"
calls | grep -q '^safe-merge' && fail "non-approver re-armed"
statuses $H1 "$(st 7 success 'github-actions[bot]')"
rm -f "${STUB_DIR}/calls"
OUT="$(env REVIEW_APPROVERS='aepfli,github-actions[bot]' "${SWEEP}" 2>/dev/null)" || true
calls | grep -q '^safe-merge' && fail "bot creator re-armed"
statuses $H1 "$(st 7 success aepfli 'CI Complete')"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#45 EJECTED"*"no \"Reviewed head\" status"*"next: delta review"* ]] || fail "no-status line"
calls | grep -q '^safe-merge' && fail "unreviewed head re-armed"
ok

# 5. PRs not ours are never touched: UNMERGEABLE release-please entry, ejected
#    renovate PR with a success status by aepfli.
reset
prs "$(opr 50 'app/release-please' $H1 UNMERGEABLE),$(opr 51 'app/renovate' $H2 '' no failed_checks)" \
    "$(mpr 52 'app/renovate' '"## Merge notes\nclose #9: done"')"
statuses $H2 "$(st 9 success aepfli)"
run
[[ $RC -eq 0 ]] || fail "not ours: rc=$RC"
calls | grep -qE 'dequeue|^safe-merge|pr comment|issue close|issue comment|statuses|comments' && fail "touched a PR not ours"
[[ "$OUT" != *"#50"* && "$OUT" != *"#51"* && "$OUT" != *"#52"* ]] || fail "not-ours lines"
# Named explicitly: reported as skipped, still untouched.
rm -f "${STUB_DIR}/calls"; run 50
[[ "$OUT" == *"#50 SKIPPED"* ]] || fail "named not-ours"
calls | grep -q dequeue && fail "named not-ours dequeued"
ok

# 6. Queued, armed and never-queued PRs: reported or silent, no action.
reset
prs "$(opr 60 aepfli $H1 AWAITING_CHECKS),$(opr 61 aepfli $H2 '' yes),$(opr 62 aepfli $H3)"
run
[[ "$OUT" == *"#60 QUEUED: entry AWAITING_CHECKS"* && "$OUT" == *"#61 ARMED"* && "$OUT" != *"#62"* ]] || fail "idle lines"
calls | grep -qE 'dequeue|^safe-merge|pr comment' && fail "acted on a healthy PR"
ok

# 7. Hand dequeue (reason manual) and conflicts: not re-armed.
reset
prs "$(opr 70 aepfli $H1 '' no manual),$(opr 71 aepfli $H2 '' no failed_checks CONFLICTING),$(opr 72 aepfli $H3 '' no merge_conflict)"
statuses $H1 "$(st 1 success aepfli)"; statuses $H2 "$(st 2 success aepfli)"; statuses $H3 "$(st 3 success aepfli)"
run
[[ "$OUT" == *"#70 DEQUEUED: dequeued by hand"* ]] || fail "manual line"
[[ "$OUT" == *"#71 EJECTED"*"next: rebase on main"* && "$OUT" == *"#72 EJECTED"*"next: rebase on main"* ]] || fail "conflict lines"
calls | grep -q '^safe-merge' && fail "manual/conflict re-armed"
ok

# 8. Fail closed: unreadable statuses or comments, failed marker post, PR-list error.
reset; prs "$(opr 80 aepfli $H1 '' no failed_checks)"; touch "${STUB_DIR}/fail-statuses-$H1"
run
[[ $RC -eq 1 && "$OUT" == *"cannot read the statuses"* ]] || fail "status API error: rc=$RC"
calls | grep -q '^safe-merge' && fail "re-armed on a status API error"
rm -f "${STUB_DIR}/fail-statuses-$H1" "${STUB_DIR}/calls"; statuses $H1 "$(st 1 success aepfli)"; touch "${STUB_DIR}/fail-comments-80"
run
[[ $RC -eq 1 ]] && ! calls | grep -q '^safe-merge' || fail "re-armed on a comments API error"
rm -f "${STUB_DIR}/fail-comments-80" "${STUB_DIR}/calls"; touch "${STUB_DIR}/fail-comment"
run
[[ $RC -eq 1 ]] && ! calls | grep -q '^safe-merge' || fail "re-armed without a marker"
reset; touch "${STUB_DIR}/fail-prs"; run
[[ $RC -eq 1 && "$OUT" == "SWEEP ERROR"* ]] || fail "list error: rc=$RC"
ok

# 9. safe-merge --arm refuses: RE-ARM FAILED, exit 1.
reset; prs "$(opr 90 aepfli $H1 '' no failed_checks)"; statuses $H1 "$(st 1 success aepfli)"; echo 2 >"${STUB_DIR}/sm-rc"
run
[[ $RC -eq 1 && "$OUT" == *"#90 RE-ARM FAILED"*"exit 2"* ]] || fail "arm refused: rc=$RC"
ok

# 10. --dry-run changes nothing.
reset; prs "$(opr 42 aepfli $H1 UNMERGEABLE),$(opr 43 aepfli $H2 '' no failed_checks)" \
  "$(mpr 44 aepfli '"## Merge notes\nclose #7: done"')"
statuses $H2 "$(st 5 success aepfli)"
run --dry-run
[[ $RC -eq 0 && "$OUT" == *"would dequeue"* && "$OUT" == *"would re-arm"* && "$OUT" == *"would close #7"* ]] || fail "dry run lines"
calls | grep -qE 'dequeuePullRequest|^safe-merge|pr comment|issue close|issue comment' && fail "dry run acted"
ok

# 11. Merged PR of ours: Merge notes close / note once; HTML comment lines ignored; old merges skipped.
reset
body='"## Summary\nx\n\n## Merge notes\n<!--\nclose #1: example from the template\n-->\n- close #7: Delivered the sweep ({MERGE})\nnote #8: follow-up lands in PR3\n\n## Related Issues\nclose #99: not in the section"'
prs "" "$(mpr 100 aepfli "$body"),$(mpr 101 aepfli '"## Merge notes\nclose #5: old"' 2026-01-01T00:00:00Z)"
run
[[ $RC -eq 0 && "$OUT" == *"#100 MERGED: merge abcdef01, branch feat/x-100; merge notes: closed #7,noted #8"* ]] || fail "merged line"
calls | grep -qF 'issue close 7 --comment Delivered the sweep (abcdef01)' || fail "close #7 with {MERGE}"
calls | grep -qF '<!-- merge-sweep close PR#100 -->' || fail "close marker"
calls | grep -qF 'issue comment 8 --body follow-up lands in PR3' || fail "note #8"
calls | grep -qE 'issue (close|view) (1|99|5)( |$)' && fail "closed an issue outside the section or an old merge"
# Second pass: #7 is closed, #8 carries the marker -> nothing again.
echo CLOSED >"${STUB_DIR}/issue-7.state"
printf '[{"body":"x\\n<!-- merge-sweep note PR#100 -->"}]' >"${STUB_DIR}/comments-8.json"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#7 already closed,#8 already noted"* ]] || fail "idempotent merged line"
calls | grep -qE 'issue close|issue comment' && fail "merge notes acted twice"
ok

echo "test-merge-sweep.sh: ${pass} groups passed"
