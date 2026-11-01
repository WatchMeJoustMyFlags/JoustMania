#!/usr/bin/env bash
# Self-contained tests for scripts/ci/merge-sweep.sh (#1809).
#
# A stub `gh` on PATH serves the PR list, merged-PR search, statuses, comments
# and issue fixtures and records every call; a stub safe-merge records --rearm
# calls. One group runs the REAL safe-merge.sh against the same stub (the revoke
# race). No network or real PR is touched.
#
#   ./scripts/tests/test-merge-sweep.sh
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/hermetic.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SWEEP="${REPO_ROOT}/scripts/ci/merge-sweep.sh"
REAL_SAFE_MERGE="${REPO_ROOT}/scripts/ci/safe-merge.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT
export STUB_DIR="${WORK}/stub"
mkdir -p "${STUB_DIR}" "${WORK}/bin"
export SWEEP_LOCK="${WORK}/sweep.lock"
export SAFE_MERGE_TEST=1 SAFE_MERGE_POLL=0 SAFE_MERGE_SETTLE=0 SAFE_MERGE_CONFIRM=0

H1=1111111111111111111111111111111111111111
H2=2222222222222222222222222222222222222222
H3=3333333333333333333333333333333333333333

# Stub gh. Fixtures in $STUB_DIR:
#   prs.json                   open-PR query answer (GraphQL)
#   merged.json / merged-2.json  merged-PR search, page 1 / page 2 (after=)
#   statuses-<sha>.json        commit statuses (missing: []); statuses-<sha>.later.json
#                              is served from the 2nd read on (a status flip)
#   comments-<n>.json          issue/PR comments (missing: [])
#   issue-<n>.json             issue object (missing: open issue)
#   closer-<n>.json            last ClosedEvent of issue n (GraphQL; missing: none)
#   rules.json, view.json, queue.json   for the real safe-merge (--rearm)
#   fail-<key>                 make that call fail: prs, merged, dequeue, statuses-<sha>,
#                              comments-<n>, comment, issue-<n>, closer-<n>
cat >"${WORK}/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >>"$STUB_DIR/calls"
jqexpr=""; prev=""; query=""; after=""; prarg=""; numarg=""
for a in "$@"; do
  [[ "$prev" == "--jq" ]] && jqexpr="$a"
  [[ "$prev" == "-f" && "$a" == query=* ]] && query="$a"
  [[ "$prev" == "-f" && "$a" == after=* ]] && after="${a#after=}"
  [[ "$prev" == "-F" && "$a" == pr=* ]] && prarg="${a#pr=}"
  [[ "$prev" == "-F" && "$a" == num=* ]] && numarg="${a#num=}"
  prev="$a"
done
out() { if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr" "$1"; else cat "$1"; fi; }
json_or() { local f="$1"; [[ -f "$f" ]] || { f="$STUB_DIR/.default"; echo "$2" >"$f"; }; out "$f"; }
case "$1 $2" in
  "api graphql")
    if [[ "$query" == *dequeuePullRequest* ]]; then
      [[ -f "$STUB_DIR/fail-dequeue" ]] && exit 1
      echo '{"data":{"dequeuePullRequest":{"clientMutationId":null}}}'
    elif [[ "$query" == *"search("* ]]; then
      [[ -f "$STUB_DIR/fail-merged" ]] && exit 1
      if [[ -n "$after" ]]; then json_or "$STUB_DIR/merged-2.json" '{"data":{"search":{"pageInfo":{"hasNextPage":false},"nodes":[]}}}'
      else json_or "$STUB_DIR/merged.json" '{"data":{"search":{"pageInfo":{"hasNextPage":false},"nodes":[]}}}'; fi
    elif [[ "$query" == *ClosedEvent* ]]; then   # who closed issue $numarg last
      [[ -f "$STUB_DIR/fail-closer-$numarg" ]] && exit 1
      json_or "$STUB_DIR/closer-$numarg.json" '{"data":{"repository":{"issue":{"timelineItems":{"nodes":[]}}}}}'
    elif [[ -n "$prarg" ]]; then
      out "$STUB_DIR/queue.json"   # safe-merge queue read
    else
      [[ -f "$STUB_DIR/fail-prs" ]] && exit 1
      out "$STUB_DIR/prs.json"
    fi ;;
  "api repos/{owner}/{repo}") echo "o/r" ;;
  "api repos/{owner}/{repo}/rules/branches/main") out "$STUB_DIR/rules.json" ;;
  "api repos/{owner}/{repo}/commits/"*)
    sha="${2#repos/\{owner\}/\{repo\}/commits/}"; sha="${sha%%/*}"
    [[ -f "$STUB_DIR/fail-statuses-$sha" ]] && exit 1
    echo x >>"$STUB_DIR/reads-$sha"
    f="$STUB_DIR/statuses-$sha.json"
    if [[ -f "$STUB_DIR/statuses-$sha.later.json" && $(wc -l <"$STUB_DIR/reads-$sha") -ge 2 ]]; then
      f="$STUB_DIR/statuses-$sha.later.json"
    fi
    json_or "$f" '[]' ;;
  "api repos/{owner}/{repo}/issues/"*)
    rest="${2#repos/\{owner\}/\{repo\}/issues/}"; n="${rest%%/*}"
    if [[ "$rest" == */comments* && "$*" == *" body="* ]]; then   # POST a comment
      [[ -f "$STUB_DIR/fail-comment" ]] && exit 1
      echo 555
    elif [[ "$rest" == */comments* ]]; then
      [[ -f "$STUB_DIR/fail-comments-$n" ]] && exit 1
      json_or "$STUB_DIR/comments-$n.json" '[]'
    else
      [[ -f "$STUB_DIR/fail-issue-$n" ]] && exit 1
      json_or "$STUB_DIR/issue-$n.json" "{\"number\":$n,\"state\":\"open\"}"
    fi ;;
  "variable get")
    if [[ -f "$STUB_DIR/var-error" ]]; then echo "HTTP 403: Resource not accessible" >&2
    else echo "variable REVIEW_APPROVERS was not found" >&2; fi
    exit 1 ;;
  "api user") [[ -f "$STUB_DIR/fail-user" ]] && exit 1; echo sweeper ;;
  "api -X")   # DELETE of a comment
    [[ -f "$STUB_DIR/fail-delete" ]] && exit 1; exit 0 ;;
  "pr comment") [[ -f "$STUB_DIR/fail-comment" ]] && exit 1; exit 0 ;;
  "pr view") cat "$STUB_DIR/view.json" ;;
  "pr merge") exit 0 ;;
  "issue close") exit 0 ;;
  "issue comment")   # posted as the sweep: later reads of comments-<n>.json see it
    f="$STUB_DIR/comments-$3.json"; [[ -f "$f" ]] || echo '[]' >"$f"
    jq --arg b "$5" '. + [{"user":{"login":"sweeper"},"created_at":"2026-11-01T00:00:00Z","body":$b}]' "$f" >"$f.tmp" && mv "$f.tmp" "$f"
    exit 0 ;;
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
  [[ -n "${6:-}" ]] && tl="[{\"reason\":\"$6\",\"createdAt\":\"${REMOVED_AT:-2026-10-31T12:00:00Z}\"}]"
  [[ -n "${6:-}" && -n "${8:-}" ]] && tl="[{\"reason\":\"manual\",\"createdAt\":\"$8\"},${tl:1}"
  printf '{"number":%s,"id":"PR_%s","author":{"login":"%s"},"headRefOid":"%s","mergeable":"%s","autoMergeRequest":%s,"mergeQueueEntry":%s,"timelineItems":{"nodes":%s}}' \
    "$1" "$1" "$2" "$3" "${7:-MERGEABLE}" "$auto" "$q" "$tl"
}
# merged PR node: number author body(json string) [mergedAt]
mpr() {
  printf '{"number":%s,"author":{"login":"%s"},"headRefName":"feat/x-%s","mergedAt":"%s","mergeCommit":{"oid":"abcdef0123456789abcdef0123456789abcdef01"},"body":%s}' \
    "$1" "$2" "$1" "${4:-$(date -u +%FT%TZ)}" "$3"
}
prs() {  # open-nodes (comma-joined) [has-next-page]
  printf '{"data":{"repository":{"pullRequests":{"pageInfo":{"hasNextPage":%s},"nodes":[%s]}}}}' "${2:-false}" "$1" >"${STUB_DIR}/prs.json"
}
merged() {  # nodes [has-next] [file]
  printf '{"data":{"search":{"pageInfo":{"hasNextPage":%s,"endCursor":"C1"},"nodes":[%s]}}}' "${2:-false}" "$1" >"${STUB_DIR}/${3:-merged.json}"
}
cm() { printf '{"user":{"login":"%s"},"created_at":"%s","body":"%s"}' "$1" "$2" "$3"; }  # author at body
comments_fx() { local n="$1"; shift; local IFS=,; printf '[%s]' "$*" >"${STUB_DIR}/comments-$n.json"; }
st() { printf '{"id":%s,"state":"%s","context":"%s","creator":{"login":"%s"}}' "$1" "$2" "${4:-Reviewed head}" "$3"; }
statuses() { local sha="$1"; shift; local IFS=,; printf '[%s]' "$*" >"${STUB_DIR}/statuses-$sha.json"; }
reset() { rm -f "${STUB_DIR}"/* "${STUB_DIR}"/.default; }
run() {  # [args] -> OUT, RC
  set +e
  OUT="$(env -u SWEEP_AUTHORS -u REVIEW_APPROVERS timeout 30 "${SWEEP}" "$@" 2>"${WORK}/stderr")"
  RC=$?
  set -e
}

# 1. UNMERGEABLE entry of ours: dequeue marker, dequeue, then re-armed once via --rearm.
reset; prs "$(opr 42 aepfli $H1 UNMERGEABLE no failed_checks)"
statuses $H1 "$(st 900 success aepfli)"
run
[[ $RC -eq 0 ]] || fail "unmergeable: rc=$RC"
calls | grep -q 'api graphql -f query=mutation.*dequeuePullRequest.* -f id=PR_42' || fail "no dequeue of PR_42"
calls | grep -qF "<!-- merge-sweep dequeued $H1 -->" || fail "no dequeue marker"
[[ "$(calls | grep -n 'merge-sweep dequeued' | cut -d: -f1)" -lt "$(calls | grep -n 'dequeuePullRequest' | cut -d: -f1)" ]] || fail "dequeue marker after dequeue"
calls | grep -qF "<!-- merge-sweep rearm $H1 -->" || fail "no re-arm marker"
calls | grep -qxF "safe-merge 42 $H1 --rearm" || fail "no --rearm"
calls | grep -q -- '--arm' && fail "the sweep must never post a status (--arm)"
[[ "$(calls | grep -n 'merge-sweep rearm' | cut -d: -f1)" -lt "$(calls | grep -n '^safe-merge 42' | cut -d: -f1)" ]] || fail "marker after arm"
[[ "$OUT" == *"#42 RE-ARMED: dequeued by this sweep (UNMERGEABLE)"* ]] || fail "unmergeable line"
ok

# 1b. dequeue fails, or its marker cannot be posted: reported, exit 1, no re-arm.
reset; prs "$(opr 42 aepfli $H1 UNMERGEABLE)"; statuses $H1 "$(st 900 success aepfli)"; touch "${STUB_DIR}/fail-dequeue"
run
[[ $RC -eq 1 && "$OUT" == *"#42 DEQUEUE FAILED"*"dequeue marker removed"* ]] || fail "dequeue failure: rc=$RC"
calls | grep -qF 'api -X DELETE repos/{owner}/{repo}/issues/comments/555' || fail "marker not deleted after a failed dequeue"
calls | grep -q '^safe-merge' && fail "re-armed after a failed dequeue"
touch "${STUB_DIR}/fail-delete"; rm -f "${STUB_DIR}/calls"; run
[[ $RC -eq 1 && "$OUT" == *"dequeue marker comment 555 NOT removed"* ]] || fail "failed marker delete not reported"
rm -f "${STUB_DIR}/fail-delete"
rm -f "${STUB_DIR}/fail-dequeue" "${STUB_DIR}/calls"; touch "${STUB_DIR}/fail-comment"
run
[[ $RC -eq 1 && "$OUT" == *"could not post the dequeue marker"* ]] || fail "dequeue marker failure: rc=$RC"
calls | grep -qE 'dequeuePullRequest|^safe-merge' && fail "dequeued without a marker"
ok

# 1c. Next pass after a sweep dequeue whose re-arm did not happen: reason manual
#     + the dequeued marker = a sweep dequeue -> re-armable; without it: by hand.
reset; prs "$(opr 42 aepfli $H1 '' no manual)"; statuses $H1 "$(st 900 success aepfli)"
comments_fx 42 "$(cm sweeper 2026-10-31T11:59:58Z "x <!-- merge-sweep dequeued $H1 -->")"
run
[[ "$OUT" == *"#42 RE-ARMED: dequeued by an earlier sweep"* ]] || fail "manual + marker not re-armed"
comments_fx 42 "$(cm aepfli 2026-10-31T11:59:58Z "<!-- merge-sweep dequeued $H1 -->")"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#42 RE-ARMED"* ]] || fail "approver-written marker not accepted"
comments_fx 42 "$(cm sweeper 2026-10-31T11:59:58Z "<!-- merge-sweep dequeued $H2 -->")"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#42 DEQUEUED: dequeued by hand"* ]] || fail "marker of another head counted"
calls | grep -q '^safe-merge' && fail "hand dequeue re-armed"
# Forged: the repo is public, a marker by anyone else never counts.
comments_fx 42 "$(cm mallory 2026-10-31T11:59:58Z "<!-- merge-sweep dequeued $H1 -->")"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#42 DEQUEUED: dequeued by hand"* ]] || fail "forged-author marker accepted"
calls | grep -q '^safe-merge' && fail "forged marker re-armed"
# Stale: a sweep marker from before an EARLIER removal does not cover a later hand dequeue.
prs "$(REMOVED_AT=2026-10-31T13:00:00Z opr 42 aepfli $H1 '' no manual MERGEABLE 2026-10-31T12:00:00Z)"
comments_fx 42 "$(cm sweeper 2026-10-31T11:59:58Z "<!-- merge-sweep dequeued $H1 -->")"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#42 DEQUEUED: dequeued by hand"* ]] || fail "stale marker accepted"
calls | grep -q '^safe-merge' && fail "stale marker re-armed"
# ... while a marker between the two removals (the latest removal was ours) counts.
comments_fx 42 "$(cm sweeper 2026-10-31T12:59:58Z "<!-- merge-sweep dequeued $H1 -->")"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#42 RE-ARMED"* ]] || fail "marker between removals not accepted"
grep -q 'WARNING' "${WORK}/stderr" && fail "warning for an unset repo variable"
touch "${STUB_DIR}/var-error"; rm -f "${STUB_DIR}/calls"; run
grep -q 'WARNING: cannot read the repo variable REVIEW_APPROVERS (HTTP 403' "${WORK}/stderr" || fail "no warning for a variable read error"
[[ "$OUT" == *"#42 RE-ARMED"* ]] || fail "default approvers after a variable read error"
rm -f "${STUB_DIR}/var-error"
# Upper bound: a marker newer than the latest removal (e.g. left by a pass whose
# dequeue failed) never covers that removal.
comments_fx 42 "$(cm sweeper 2026-10-31T13:00:05Z "<!-- merge-sweep dequeued $H1 -->")"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#42 DEQUEUED: dequeued by hand"* ]] || fail "marker after the latest removal accepted"
calls | grep -q '^safe-merge' && fail "marker after the latest removal re-armed"
ok

# 2. Ejected (failed_checks), success by approver: re-armed once ...
reset; prs "$(opr 43 aepfli $H2 '' no failed_checks)"; statuses $H2 "$(st 5 success aepfli)"
run
[[ $RC -eq 0 && "$OUT" == *"#43 RE-ARMED: removed from the queue (failed_checks)"* ]] || fail "ejected re-arm: rc=$RC"
calls | grep -qxF "safe-merge 43 $H2 --rearm" || fail "no re-arm of 43"
# ... and not a second time when the marker for this head exists.
comments_fx 43 "$(cm sweeper 2026-10-31T12:01:00Z "Merge sweep: re-armed <!-- merge-sweep rearm $H2 -->")"
rm -f "${STUB_DIR}/calls"
run
[[ "$OUT" == *"#43 EJECTED: removed from the queue (failed_checks) again; already re-armed once"*"next: fix the failing test"* ]] || fail "second re-arm line"
calls | grep -q '^safe-merge' && fail "re-armed twice at one head"
calls | grep -q 'pr comment' && fail "second marker posted"
# A marker for an older head does not block the new head.
comments_fx 43 "$(cm sweeper 2026-10-31T12:01:00Z "<!-- merge-sweep rearm $H1 -->")"
rm -f "${STUB_DIR}/calls"
run
calls | grep -qxF "safe-merge 43 $H2 --rearm" || fail "old-head marker blocked the new head"
# Markers count only from trusted logins (consistent with the dequeue marker).
comments_fx 43 "$(cm mallory 2026-10-31T12:01:00Z "<!-- merge-sweep rearm $H2 -->")"
rm -f "${STUB_DIR}/calls"
run
calls | grep -qxF "safe-merge 43 $H2 --rearm" || fail "untrusted rearm marker counted"
ok

# 3. No re-arm when the newest status is failure (revoke), even with an older success.
reset; prs "$(opr 44 aepfli $H3 '' no failed_checks)"
statuses $H3 "$(st 901 failure aepfli)" "$(st 900 success aepfli)"
run
[[ "$OUT" == *"#44 EJECTED"*"newest \"Reviewed head\" on 33333333 is failure"* ]] || fail "revoked line"
calls | grep -qE '^safe-merge|pr comment' && fail "revoked PR re-armed"
statuses $H3 "$(st 900 success aepfli)" "$(st 901 failure aepfli)"
rm -f "${STUB_DIR}/calls"; run
calls | grep -q '^safe-merge' && fail "revoke listed second still re-armed"
ok

# 3b. Revoke race, with the REAL safe-merge: the sweep reads success, then the
#     status flips to failure before the arm -> --rearm re-reads, no arm, no POST.
reset; prs "$(opr 46 aepfli $H1 '' no failed_checks)"
statuses $H1 "$(st 900 success aepfli)"
printf '[%s,%s]' "$(st 901 failure aepfli)" "$(st 900 success aepfli)" >"${STUB_DIR}/statuses-$H1.later.json"
printf '[{"type":"required_status_checks","parameters":{"required_status_checks":[{"context":"CI Complete"},{"context":"Reviewed head"}]}},{"type":"merge_queue","parameters":{"grouping_strategy":"ALLGREEN"}}]' >"${STUB_DIR}/rules.json"
printf '{"headRefOid":"%s","state":"OPEN","mergeable":"MERGEABLE","statusCheckRollup":[]}' "$H1" >"${STUB_DIR}/view.json"
printf '{"data":{"repository":{"pullRequest":{"state":"OPEN","headRefOid":"%s","isInMergeQueue":true,"url":"u","mergeQueueEntry":{"state":"QUEUED"},"mergeCommit":null,"autoMergeRequest":null}}}}' "$H1" >"${STUB_DIR}/queue.json"
set +e
OUT="$(env -u SWEEP_AUTHORS -u REVIEW_APPROVERS SWEEP_SAFE_MERGE="$REAL_SAFE_MERGE" timeout 30 "${SWEEP}" 2>"${WORK}/stderr")"; RC=$?
set -e
[[ $RC -eq 1 && "$OUT" == *"#46 RE-ARM FAILED"*"NOT RE-ARMED (head 11111111): newest \"Reviewed head\" is failure"* ]] || fail "race: rc=$RC"
calls | grep -q 'pr merge' && fail "revoked head armed in the race"
calls | grep -q 'api -X POST' && fail "a status was posted in the race"
# Control: no flip -> the real safe-merge arms (and still posts nothing).
rm -f "${STUB_DIR}/statuses-$H1.later.json" "${STUB_DIR}/calls" "${STUB_DIR}/reads-$H1" "${STUB_DIR}/comments-46.json"
set +e
OUT="$(env -u SWEEP_AUTHORS -u REVIEW_APPROVERS SWEEP_SAFE_MERGE="$REAL_SAFE_MERGE" timeout 30 "${SWEEP}" 2>"${WORK}/stderr")"; RC=$?
set -e
[[ $RC -eq 0 && "$OUT" == *"#46 RE-ARMED"* ]] || fail "real rearm control: rc=$RC $(cat "${WORK}/stderr")"
calls | grep -qF "pr merge 46 --auto --squash --match-head-commit $H1" || fail "control did not arm"
calls | grep -q 'api -X POST' && fail "control posted a status"
ok

# 4. No re-arm when the newest success is by a non-approver or a bot, or there is no status.
reset; prs "$(opr 45 aepfli $H1 '' no failed_checks)"; statuses $H1 "$(st 7 success mallory)"
run
[[ "$OUT" == *"#45 EJECTED"*"by mallory, not an approver"* ]] || fail "non-approver line"
calls | grep -q '^safe-merge' && fail "non-approver re-armed"
# A glob in the approver list matches literally only.
rm -f "${STUB_DIR}/calls"
OUT="$(env REVIEW_APPROVERS='*,mall*' "${SWEEP}" 2>/dev/null)" || true
calls | grep -q '^safe-merge' && fail "glob approver matched"
statuses $H1 "$(st 7 success 'github-actions[bot]')"
rm -f "${STUB_DIR}/calls"
OUT="$(env REVIEW_APPROVERS='aepfli,github-actions[bot]' "${SWEEP}" 2>/dev/null)" || true
calls | grep -q '^safe-merge' && fail "bot creator re-armed"
statuses $H1 "$(st 7 success aepfli 'CI Complete')"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#45 EJECTED"*"no \"Reviewed head\" status"*"next: delta review"* ]] || fail "no-status line"
calls | grep -q '^safe-merge' && fail "unreviewed head re-armed"
ok

# 5. PRs not ours are never touched.
reset
prs "$(opr 50 'app/release-please' $H1 UNMERGEABLE),$(opr 51 'app/renovate' $H2 '' no failed_checks)"
merged "$(mpr 52 'app/renovate' '"Part of #9\n## Merge notes\nclose #9: done"')"
statuses $H2 "$(st 9 success aepfli)"
run
[[ $RC -eq 0 ]] || fail "not ours: rc=$RC"
calls | grep -qE 'dequeue|^safe-merge|pr comment|issue close|issue comment|statuses|comments|issues/' && fail "touched a PR not ours"
[[ "$OUT" != *"#50"* && "$OUT" != *"#51"* && "$OUT" != *"#52"* ]] || fail "not-ours lines"
calls | grep -qF 'is:pr is:merged author:aepfli merged:>=' || fail "merged search not scoped to our author"
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

# 7. Conflicts: not re-armed.
reset
prs "$(opr 71 aepfli $H2 '' no failed_checks CONFLICTING),$(opr 72 aepfli $H3 '' no merge_conflict)"
statuses $H2 "$(st 2 success aepfli)"; statuses $H3 "$(st 3 success aepfli)"
run
[[ "$OUT" == *"#71 EJECTED"*"next: rebase on main"* && "$OUT" == *"#72 EJECTED"*"next: rebase on main"* ]] || fail "conflict lines"
calls | grep -q '^safe-merge' && fail "conflict re-armed"
ok

# 8. Fail closed: unreadable statuses or comments, failed marker post, list errors.
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
reset; prs ""; touch "${STUB_DIR}/fail-merged"; run
[[ $RC -eq 1 && "$OUT" == *"SWEEP WARNING: cannot read the PRs merged by aepfli"* ]] || fail "merged search error: rc=$RC"
ok

# 9. safe-merge --rearm refuses: RE-ARM FAILED, exit 1.
reset; prs "$(opr 90 aepfli $H1 '' no failed_checks)"; statuses $H1 "$(st 1 success aepfli)"; echo 1 >"${STUB_DIR}/sm-rc"
run
[[ $RC -eq 1 && "$OUT" == *"#90 RE-ARM FAILED"*"exit 1"* ]] || fail "arm refused: rc=$RC"
ok

# 10. --dry-run changes nothing.
reset; prs "$(opr 42 aepfli $H1 UNMERGEABLE),$(opr 43 aepfli $H2 '' no failed_checks)"
merged "$(mpr 44 aepfli '"Part of #7\n## Merge notes\nclose #7: done"')"
statuses $H2 "$(st 5 success aepfli)"
run --dry-run
[[ $RC -eq 0 && "$OUT" == *"would dequeue"* && "$OUT" == *"would re-arm"* && "$OUT" == *"would close #7"* ]] || fail "dry run lines"
calls | grep -qE 'dequeuePullRequest|^safe-merge|pr comment|issue close|issue comment' && fail "dry run acted"
ok

# 11. Merged PR of ours: close / note once; HTML comment lines ignored; old merges skipped.
reset
body='"## Summary\nx\n\n## Related Issues\n\nPart of #7, #8\n\n## Merge notes\n<!--\nclose #1: example from the template\n-->\n- close #7: Delivered the sweep ({MERGE})\nnote #8: follow-up lands in PR3\n\n## Other\nclose #99: not in the section"'
merged "$(mpr 100 aepfli "$body"),$(mpr 101 aepfli '"Part of #5\n## Merge notes\nclose #5: old"' 2026-01-01T00:00:00Z)"
prs ""
run
[[ $RC -eq 0 && "$OUT" == *"#100 MERGED: merge abcdef01, branch feat/x-100; merge notes: closed #7,noted #8"* ]] || fail "merged line"
calls | grep -qF 'issue close 7 --comment Delivered the sweep (abcdef01)' || fail "close #7 with {MERGE}"
calls | grep -qF '<!-- merge-sweep close PR#100 -->' || fail "close marker"
calls | grep -qF 'issue comment 8 --body follow-up lands in PR3' || fail "note #8"
calls | grep -qE 'issue (close|comment) (1|99|5)( |$)|issues/(1|99|5)( |/|$)' && fail "acted outside the section or on an old merge"
# Second pass: #8 carries the note marker -> no second note; #7 closed -> skipped.
echo '{"number":7,"state":"closed"}' >"${STUB_DIR}/issue-7.json"
comments_fx 8 "$(cm sweeper 2026-10-31T12:00:00Z "x <!-- merge-sweep note PR#100 -->")"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#7 already closed,#8 already noted"* ]] || fail "idempotent merged line"
calls | grep -qE 'issue close|issue comment' && fail "merge notes acted twice"
ok

# 11b. A reopened issue (or a failed close after its comment) stays open and is
#      reported: close marker present + state open -> "check issue".
echo '{"number":7,"state":"open"}' >"${STUB_DIR}/issue-7.json"
comments_fx 7 "$(cm sweeper 2026-10-31T12:00:00Z "Delivered <!-- merge-sweep close PR#100 -->")" "$(cm aepfli 2026-10-31T13:00:00Z "reopened: not done")"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#7 has the sweep's close comment but is open"*"next: check issue"* ]] || fail "reopened line"
[[ "$OUT" != *"#7 already closed"* ]] || fail "open issue reported as closed"
calls | grep -q 'issue close 7' && fail "re-closed a reopened issue"
# Closed + marker: already closed by the sweep. A forged close marker is ignored (closes normally).
echo '{"number":7,"state":"closed"}' >"${STUB_DIR}/issue-7.json"
rm -f "${STUB_DIR}/calls"; run
[[ "$OUT" == *"#7 already closed by the sweep"* ]] || fail "closed + marker line"
echo '{"number":7,"state":"open"}' >"${STUB_DIR}/issue-7.json"
comments_fx 7 "$(cm mallory 2026-10-31T12:00:00Z "<!-- merge-sweep close PR#100 -->")"
rm -f "${STUB_DIR}/calls"; run
calls | grep -qF 'issue close 7 --comment' || fail "forged close marker blocked the close"
ok

# 11c. Targets: only issues on the Part of line, never a pull request.
reset; prs ""
merged "$(mpr 110 aepfli '"Part of #7 (follow-up of #9)\n## Merge notes\nclose #9: not on the Part of line\nclose #7: done"')"
echo '{"number":7,"state":"open","pull_request":{"url":"x"}}' >"${STUB_DIR}/issue-7.json"
run
[[ "$OUT" == *"#9 refused (not on the Part of line)"* && "$OUT" == *"#7 refused (a pull request)"*"next: check the merge notes"* ]] || fail "target refusal lines"
calls | grep -qE 'issue close|issue comment' && fail "closed a refused target"
ok

# 11d. `close #N` is a GitHub closing keyword: the merge itself closed the issue
#      (#2508). Closer = this PR or its merge commit -> delivery comment once
#      (no close call); a second pass posts nothing; closed by anything else or
#      no ClosedEvent -> "already closed", no comment; unreadable closer -> fail closed.
closer_fx() {  # issue typename value | issue null (a hand close: closer null)
  local c='{"__typename":"'"$2"'","number":'"${3:-}"'}'
  [[ "$2" == Commit ]] && c='{"__typename":"Commit","oid":"'"$3"'"}'
  [[ "$2" == ProjectV2 ]] && c='{"__typename":"ProjectV2"}'
  [[ "$2" == null ]] && c=null
  printf '{"data":{"repository":{"issue":{"timelineItems":{"nodes":[{"closer":%s}]}}}}}' "$c" >"${STUB_DIR}/closer-$1.json"
}
reset; prs ""
merged "$(mpr 130 aepfli '"Part of #7\n## Merge notes\nclose #7: Delivered the fix ({MERGE})"')"
echo '{"number":7,"state":"closed"}' >"${STUB_DIR}/issue-7.json"
closer_fx 7 PullRequest 130
run
[[ $RC -eq 0 && "$OUT" == *"#130 MERGED: merge abcdef01, branch feat/x-130; merge notes: commented #7 (closed by this merge); next: none"* ]] || fail "auto-closed line: rc=$RC"
[[ "$(calls | grep -c '^gh issue comment 7 --body Delivered the fix (abcdef01)')" -eq 1 ]] || fail "auto-closed: not exactly one delivery comment"
calls | grep -qF '<!-- merge-sweep close PR#130 -->' || fail "auto-closed: no marker"
calls | grep -q 'issue close' && fail "auto-closed: closed an already closed issue"
# Run twice: the marker from the first pass -> nothing more.
rm -f "${STUB_DIR}/calls"; run
[[ $RC -eq 0 && "$OUT" == *"#7 already closed by the sweep"* ]] || fail "second pass line: rc=$RC"
calls | grep -qE 'issue (close|comment)' && fail "second pass commented again"
[[ "$(jq length "${STUB_DIR}/comments-7.json")" -eq 1 ]] || fail "more than one delivery comment after two passes"
# Closed by the merge commit: same.
rm -f "${STUB_DIR}/calls" "${STUB_DIR}/comments-7.json"; closer_fx 7 Commit abcdef0123456789abcdef0123456789abcdef01
run
[[ "$(calls | grep -c '^gh issue comment 7 ')" -eq 1 ]] || fail "merge-commit closer: no delivery comment"
# Closed by someone else (another PR, another commit, a project, a person = closer
# null, or no event): skipped.
for c in "PullRequest 999" "Commit 9999999999999999999999999999999999999999" ProjectV2 null none; do
  rm -f "${STUB_DIR}/calls" "${STUB_DIR}/comments-7.json" "${STUB_DIR}/closer-7.json"
  [[ "$c" == none ]] || closer_fx 7 $c
  run
  [[ $RC -eq 0 && "$OUT" == *"merge notes: #7 already closed; next: none"* ]] || fail "closed by $c: line rc=$RC"
  calls | grep -qE 'issue (close|comment)' && fail "closed by $c: commented"
done
# A forged close marker (untrusted login) does not count on this path either: one comment.
closer_fx 7 PullRequest 130; rm -f "${STUB_DIR}/calls"
comments_fx 7 "$(cm mallory 2026-10-31T12:00:00Z "<!-- merge-sweep close PR#130 -->")"
run
[[ $RC -eq 0 && "$OUT" == *"commented #7 (closed by this merge)"* ]] || fail "forged marker, auto-closed: line rc=$RC"
[[ "$(calls | grep -c '^gh issue comment 7 ')" -eq 1 ]] || fail "forged marker, auto-closed: not exactly one comment"
# A delivery comment written by hand after the merge (no marker, #2509): a trusted
# comment at or after mergedAt naming #130 as a whole token -> no sweep comment.
merged_at="$(date -u -d '-1 hour' +%FT%TZ)" after="$(date -u -d '-10 minutes' +%FT%TZ)"
before="$(date -u -d '-2 hours' +%FT%TZ)"
merged "$(mpr 130 aepfli '"Part of #7\n## Merge notes\nclose #7: Delivered the fix ({MERGE})"' "$merged_at")"
hand_case() {  # login at body expected-comments label
  rm -f "${STUB_DIR}/calls"; comments_fx 7 "$(cm "$1" "$2" "$3")"; run
  [[ $RC -eq 0 ]] || fail "$5: rc=$RC"
  [[ "$(calls | grep -c '^gh issue comment 7 ')" -eq "$4" ]] || fail "$5: expected $4 sweep comment(s)"
}
hand_case aepfli "$after" "Delivered by #130 (merge abcdef01)" 0 "trusted hand comment after the merge"
[[ "$OUT" == *"#7 already has a delivery comment; next: none"* ]] || fail "hand comment line"
hand_case aepfli "$merged_at" "Delivered by #130" 0 "trusted hand comment at mergedAt"
hand_case mallory "$after" "Delivered by #130 (merge abcdef01)" 1 "untrusted hand comment"
hand_case aepfli "$after" "Delivered by #1304 and #13" 1 "trusted comment naming another PR"
hand_case aepfli "$before" "Delivered by #130" 1 "trusted comment before the merge"
# Dry run: would comment; unreadable closer: fail closed, no comment.
rm -f "${STUB_DIR}/comments-7.json"
closer_fx 7 PullRequest 130; rm -f "${STUB_DIR}/calls"; run --dry-run
[[ "$OUT" == *"would comment #7 (closed by this merge)"* ]] || fail "dry-run auto-closed line"
calls | grep -qE 'issue (close|comment)' && fail "dry run commented"
touch "${STUB_DIR}/fail-closer-7"; rm -f "${STUB_DIR}/calls"; run
[[ $RC -eq 1 && "$OUT" == *"close #7: read failed"*"next: re-check later"* ]] || fail "unreadable closer: rc=$RC"
calls | grep -qE 'issue (close|comment)' && fail "commented without reading the closer"
ok

# 12. Merged search: all pages read; a result that may be truncated is a failure.
reset; prs ""
merged "$(mpr 120 aepfli '"no notes"')" true
merged "$(mpr 121 aepfli '"no notes"')" false merged-2.json
run
[[ $RC -eq 0 && "$OUT" == *"#120 MERGED"* && "$OUT" == *"#121 MERGED"* ]] || fail "second page not read: rc=$RC"
calls | grep -q -- '-f after=C1' || fail "no cursor on page 2"
merged "$(mpr 121 aepfli '"no notes"')" true merged-2.json   # every page says "more"
run
[[ $RC -eq 1 && "$OUT" == *"SWEEP WARNING: more than 500 PRs merged"* ]] || fail "truncated merged list: rc=$RC"
reset; prs "$(opr 62 aepfli $H3)" true; run
[[ $RC -eq 1 && "$OUT" == *"more than 100 open PRs"* ]] || fail "truncated open list: rc=$RC"
ok

# 13. One sweep at a time: a held lock -> SWEEP BUSY, nothing read.
reset; prs ""
exec 8>>"$SWEEP_LOCK"; flock -n 8
run
exec 8>&-
[[ $RC -eq 1 && "$OUT" == "SWEEP BUSY"* ]] || fail "lock: rc=$RC"
[[ -z "$(calls)" ]] || fail "busy sweep called gh"
ok

# 13b. Without SWEEP_LOCK the lock lives in the SCRIPT's repo, whatever the cwd.
reset; prs ""
# A throwaway repo holding a copy of the script: never the real clone's lock.
mkdir -p "$WORK/repo/scripts/ci" "$WORK/elsewhere"
git init -q "$WORK/repo"
cp "$SWEEP" "$WORK/repo/scripts/ci/merge-sweep.sh"
REPO_LOCK="$(git -C "$WORK/repo" rev-parse --path-format=absolute --git-common-dir)/merge-sweep.lock"
exec 8>>"$REPO_LOCK"; flock -n 8
set +e
OUT="$(cd "$WORK/elsewhere" && env -u SWEEP_LOCK -u SWEEP_AUTHORS -u REVIEW_APPROVERS timeout 30 "$WORK/repo/scripts/ci/merge-sweep.sh" 2>/dev/null)"; RC=$?
set -e
exec 8>&-
[[ $RC -eq 1 && "$OUT" == "SWEEP BUSY: another merge sweep holds $REPO_LOCK"* ]] || fail "lock from another cwd: rc=$RC"
ok

# 14. The sweep's own login cannot be read: fail closed, nothing touched.
reset; prs "$(opr 42 aepfli $H1 UNMERGEABLE)"; touch "${STUB_DIR}/fail-user"
run
[[ $RC -eq 1 && "$OUT" == "SWEEP ERROR: cannot read the sweep's own login"* ]] || fail "own login: rc=$RC"
calls | grep -qE 'graphql|comments|^safe-merge' && fail "acted without its own login"
ok

echo "test-merge-sweep.sh: ${pass} groups passed"
