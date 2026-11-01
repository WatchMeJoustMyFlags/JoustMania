#!/usr/bin/env bash
# merge-sweep.sh: one pass over our PRs in the review-gated merge flow (#1809).
#
#   merge-sweep.sh [--dry-run] [PR ...]
#
# GitHub owns merging once a reviewed head is armed (`safe-merge.sh --arm`).
# Verified on #2478: GitHub clears `autoMergeRequest` as soon as it queues a
# PR, so after ANY dequeue or ejection nothing stays armed; a failed merge-group
# check leaves the entry sitting UNMERGEABLE for minutes; and a queued branch
# rejects pushes (GH006). This pass:
#   1. Open PRs authored by SWEEP_AUTHORS ("ours"; others are never touched):
#      - queue entry UNMERGEABLE: post a `<!-- merge-sweep dequeued <sha> -->`
#        PR comment, dequeue the entry (GraphQL dequeuePullRequest), then treat
#        it as ejected (below);
#      - queued (any other entry state) or auto-merge armed: report, no action;
#      - not queued, not armed, with a RemovedFromMergeQueueEvent (dequeued or
#        ejected): re-arm with `safe-merge.sh <PR> <head> --rearm` only when ALL
#        hold:
#          * not CONFLICTING and the last removal reason is not a conflict;
#          * the NEWEST "Reviewed head" status on the current head is success,
#            posted by an approver (never a `[bot]`);
#          * the last removal was not a hand dequeue: reason `manual` counts as
#            a sweep dequeue only with the dequeued marker for this head, written
#            by the sweep's own login or an approver, between the previous
#            removal and this one;
#          * no re-arm marker for this head yet: at most ONE re-arm per head.
#            The marker is a PR comment `<!-- merge-sweep rearm <sha> -->`,
#            posted BEFORE arming (a crash never allows a second re-arm).
#        `--rearm` posts no status and re-reads the newest status right before
#        arming, so a `--revoke` that lands after this pass's read still wins.
#        Otherwise it reports why and names the next step.
#      - never queued (no removal event): not part of the flow, no line.
#   2. Our PRs merged in the last SWEEP_MERGED_HOURS (search, all pages): a
#      `MERGED` line with the branch (the caller removes its worktree), then the
#      PR body's `## Merge notes` lines (HTML comments ignored):
#        close #N: <text>   close issue N with <text>, once (marker on the issue;
#                           skipped when already closed by this sweep, so a
#                           reopened issue stays open). `close #N` is also a GitHub
#                           closing keyword: when the issue is already closed and
#                           its last ClosedEvent's closer is this PR or its merge
#                           commit, post <text> as a comment only (same marker,
#                           once, #2508), unless a trusted comment created at or
#                           after mergedAt already names #<PR> (a hand delivery
#                           comment, #2509); closed by anything else: skipped
#        note #N: <text>    comment <text> on issue N, once per PR (marker)
#      Only issues in the body's `Part of #A[, #B]` list(s) are targets, and
#      never a pull request. `{MERGE}` in <text> becomes the short merge SHA.
# Markers count only from the sweep's own login (`gh api user`) or an approver
# (the repo is public: anyone can comment). A close marker on an open issue is
# reported ("check issue"), never re-closed.
# Fails CLOSED: an unreadable status, marker list or issue never acts. A result
# list that may be truncated is a failure (exit 1), not a silent skip.
# One sweep at a time: an flock on SWEEP_LOCK (default <git common dir>/merge-sweep.lock).
#
# --dry-run: read everything, change nothing (lines say "would ...").
# PR ...:    restrict the pass to these numbers (still only ours).
#
# Output: one line per PR, `#<n> <STATE>: <detail>; next: <step>`, then a
# `SWEEP:` summary. Exit 0 pass completed (lines may still name a next step),
# 1 a read failed, a list may be truncated, an action failed or another sweep
# holds the lock; 2 usage.
#
# Env: SWEEP_AUTHORS (default aepfli); REVIEW_APPROVERS (default: the repo
#      variable REVIEW_APPROVERS via `gh variable get`, which review-gate.yml
#      reads too, else aepfli; set it locally only to the same list);
#      SWEEP_MERGED_HOURS (default 48); SWEEP_LOCK; SWEEP_SAFE_MERGE (default
#      the sibling safe-merge.sh; tests only); GH_REPO for gh.
set -uo pipefail

CONTEXT="Reviewed head"
usage() { sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }

DRY=false
ONLY=()
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=true ;;
    -h|--help) usage ;;
    *) [[ "$a" =~ ^[0-9]+$ ]] || usage; ONLY+=("$a") ;;
  esac
done

HOURS="${SWEEP_MERGED_HOURS:-48}"
[[ "$HOURS" =~ ^[0-9]+$ ]] || { echo "SWEEP_MERGED_HOURS must be a number: '$HOURS'" >&2; exit 2; }
SAFE_MERGE="${SWEEP_SAFE_MERGE:-$(cd "$(dirname "$0")" && pwd)/safe-merge.sh}"

LOCK="${SWEEP_LOCK:-}"
if [[ -z "$LOCK" ]]; then
  # The script's own repo, not the caller's cwd: every sweep of this clone shares one lock.
  gitdir="$(git -C "$(dirname "$0")" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  LOCK="${gitdir:-${TMPDIR:-/tmp}}/merge-sweep.lock"
fi
exec 9>>"$LOCK" || { echo "SWEEP ERROR: cannot open the lock $LOCK"; exit 1; }
flock -n 9 || { echo "SWEEP BUSY: another merge sweep holds $LOCK; nothing done"; exit 1; }

split_list() { local -a l; IFS=$' \t\n,' read -r -a l <<<"$1"; printf '%s\n' "${l[@]}"; }
AUTHORS="${SWEEP_AUTHORS:-}"
[[ -n "${AUTHORS//[[:space:],]/}" ]] || AUTHORS="aepfli"
APPROVERS="${REVIEW_APPROVERS:-}"
# The repo variable REVIEW_APPROVERS (what review-gate.yml reads). Not set ->
# empty, silently (the gate then defaults to aepfli too); a real read error ->
# empty plus a WARNING on stderr.
repo_approvers() {
  local out err rc=0
  err="$(mktemp)"
  out="$(gh variable get REVIEW_APPROVERS 2>"$err")" || rc=$?
  if (( rc == 0 )); then echo "$out"
  elif ! grep -qi 'not found' "$err"; then
    echo "WARNING: cannot read the repo variable REVIEW_APPROVERS ($(head -n 1 "$err")); using the default aepfli (review-gate.yml may use another list)" >&2
  fi
  rm -f "$err"
}
[[ -n "${APPROVERS//[[:space:],]/}" ]] || APPROVERS="$(repo_approvers)"
[[ -n "${APPROVERS//[[:space:],]/}" ]] || APPROVERS="aepfli"
# Marker comments count only when written by the sweep's own identity or an
# approver: the repo is public and anyone can comment a forged marker.
# Without it the sweep would ignore its own once-only markers (fail open): stop.
ME="$(gh api user --jq .login 2>/dev/null || true)"
[[ -n "$ME" ]] || { echo "SWEEP ERROR: cannot read the sweep's own login (gh api user); nothing done"; exit 1; }
TRUSTED="$APPROVERS${ME:+,$ME}"

failures=0 attention=0 lines=0
emit() {  # number state detail next
  lines=$((lines + 1))
  [[ "$4" != none ]] && attention=$((attention + 1))
  echo "#$1 $2: $3; next: $4"
}
in_list() {  # word list: exact match, no glob
  local w
  while IFS= read -r w; do [[ -n "$w" && "$1" == "$w" ]] && return 0; done < <(split_list "$2")
  return 1
}
selected() {
  [[ ${#ONLY[@]} -eq 0 ]] && return 0
  local n
  for n in "${ONLY[@]}"; do [[ "$n" == "$1" ]] && return 0; done
  return 1
}

GQL_OPEN='query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo){
  pullRequests(states:OPEN, first:100){pageInfo{hasNextPage} nodes{number id author{login} headRefOid mergeable
    autoMergeRequest{enabledAt} mergeQueueEntry{state}
    timelineItems(last:2, itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{... on RemovedFromMergeQueueEvent{reason createdAt}}}}}}}'
GQL_MERGED='query($q:String!,$after:String){search(query:$q, type:ISSUE, first:50, after:$after){
  pageInfo{hasNextPage endCursor} nodes{... on PullRequest{number author{login} headRefName mergedAt mergeCommit{oid} body}}}}'

open_data="$(gh api graphql -F owner='{owner}' -F repo='{repo}' -f query="$GQL_OPEN" 2>/dev/null)" \
  && jq -e '.data.repository.pullRequests.nodes' >/dev/null 2>&1 <<<"$open_data" \
  || { echo "SWEEP ERROR: cannot read the open PRs (API error); nothing done"; exit 1; }

# Our merged PRs since the cutoff: one search per author, every page (at most 10).
repo_full="$(gh api 'repos/{owner}/{repo}' --jq .full_name 2>/dev/null)" && [[ "$repo_full" == */* ]] \
  || { echo "SWEEP ERROR: cannot read the repository name (API error); nothing done"; exit 1; }
cutoff=$(( $(date -u +%s) - HOURS * 3600 ))
since="$(date -u -d "@$cutoff" +%Y-%m-%dT%H:%M:%SZ)"
merged_nodes=""
merged_ok=true
while IFS= read -r who; do
  [[ -n "$who" ]] || continue
  after="" page=0
  while :; do
    page=$((page + 1))
    args=(-f query="$GQL_MERGED" -f q="repo:$repo_full is:pr is:merged author:$who merged:>=$since")
    [[ -n "$after" ]] && args+=(-f after="$after")
    if ! res="$(gh api graphql "${args[@]}" 2>/dev/null)" || ! jq -e '.data.search.nodes' >/dev/null 2>&1 <<<"$res"; then
      merged_ok=false; failures=$((failures + 1))
      echo "SWEEP WARNING: cannot read the PRs merged by $who since $since (API error); merged PRs skipped"
      break
    fi
    merged_nodes+="$(jq -c '.data.search.nodes[] | select(.number)' <<<"$res")"$'\n'
    [[ "$(jq -r '.data.search.pageInfo.hasNextPage' <<<"$res")" == true ]] || break
    if (( page >= 10 )); then
      failures=$((failures + 1))
      echo "SWEEP WARNING: more than $((page * 50)) PRs merged by $who since $since; the rest was not read"
      break
    fi
    after="$(jq -r '.data.search.pageInfo.endCursor // ""' <<<"$res")"
    [[ -n "$after" ]] || break
  done
done < <(split_list "$AUTHORS")

if [[ "$(jq -r '.data.repository.pullRequests.pageInfo.hasNextPage' <<<"$open_data")" == true ]]; then
  failures=$((failures + 1))
  echo "SWEEP WARNING: more than 100 open PRs; only the first 100 were swept"
fi

# Newest "Reviewed head" status on a SHA: "<state> <creator>", "none", or exit 1 (API error).
newest_review() {
  local entries newest st by
  entries="$(gh api "repos/{owner}/{repo}/commits/$1/statuses?per_page=100" --paginate \
    --jq ".[] | select(.context == \"$CONTEXT\") | \"\(.id) \(.state) \(.creator.login // \"-\")\"" 2>/dev/null)" || return 1
  newest="$(sort -k1,1nr <<<"$entries" | sed -n '1p')"
  if [[ -z "$newest" ]]; then echo none; return 0; fi
  read -r _ st by <<<"$newest"
  echo "$st $by"
}

# All comment bodies of an issue or PR (exit 1 on API error).
# Comments of an issue or PR as JSON lines {u, at, b} (exit 1 on API error).
comments() {
  gh api "repos/{owner}/{repo}/issues/$1/comments?per_page=100" --paginate \
    --jq '.[] | {u: (.user.login // ""), at: (.created_at // ""), b: (.body // "")} | tojson' 2>/dev/null
}
# has_marker <comments> <marker> [after] [until]: a comment by a TRUSTED login
# holds the marker (created after <after> and not after <until>, ISO-8601 UTC).
has_marker() {
  local c u at
  while IFS= read -r c; do
    [[ -n "$c" ]] || continue
    jq -e --arg m "$2" '.b | contains($m)' >/dev/null 2>&1 <<<"$c" || continue
    u="$(jq -r .u <<<"$c")"; at="$(jq -r .at <<<"$c")"
    in_list "$u" "$TRUSTED" || continue
    [[ -n "${3:-}" && ! "$at" > "$3" ]] && continue
    [[ -n "${4:-}" && "$at" > "$4" ]] && continue
    return 0
  done <<<"$1"
  return 1
}

# names_pr <comments> <pr> <since>: a comment by a TRUSTED login, created at or
# after <since> (ISO-8601 UTC), names #<pr> as a whole token (#250 != #2504).
# A delivery comment written by hand after the merge has no sweep marker (#2509).
names_pr() {
  local c u at
  while IFS= read -r c; do
    [[ -n "$c" ]] || continue
    jq -e --arg n "$2" '.b | test("(^|[^0-9A-Za-z_])#" + $n + "([^0-9]|$)")' >/dev/null 2>&1 <<<"$c" || continue
    u="$(jq -r .u <<<"$c")"; at="$(jq -r .at <<<"$c")"
    in_list "$u" "$TRUSTED" || continue
    [[ "$at" < "$3" ]] && continue
    return 0
  done <<<"$1"
  return 1
}

dequeue() {  # PR node id
  gh api graphql -f query='mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}' \
    -f id="$1" >/dev/null 2>&1
}

sweep_open() {  # one open-PR JSON object
  local pr="$1" n id author head mergeable auto qstate reason short bodies
  n="$(jq -r .number <<<"$pr")"
  selected "$n" || return 0
  author="$(jq -r '.author.login // ""' <<<"$pr")"
  if ! in_list "$author" "$AUTHORS"; then
    [[ ${#ONLY[@]} -gt 0 ]] && emit "$n" SKIPPED "author $author is not in SWEEP_AUTHORS ($AUTHORS); not touched" none
    return 0
  fi
  id="$(jq -r .id <<<"$pr")"
  head="$(jq -r .headRefOid <<<"$pr")"
  mergeable="$(jq -r '.mergeable // ""' <<<"$pr")"
  auto="$(jq -r 'if .autoMergeRequest then "yes" else "no" end' <<<"$pr")"
  qstate="$(jq -r '.mergeQueueEntry.state // ""' <<<"$pr")"
  reason="$(jq -r '.timelineItems.nodes[-1].reason // ""' <<<"$pr")"
  local removed_at prev_removed_at
  removed_at="$(jq -r '.timelineItems.nodes[-1].createdAt // ""' <<<"$pr")"
  prev_removed_at="$(jq -r 'if (.timelineItems.nodes | length) >= 2 then .timelineItems.nodes[-2].createdAt // "" else "" end' <<<"$pr")"
  short="${head:0:8}"
  local dq_marker="<!-- merge-sweep dequeued $head -->"

  if [[ "$qstate" == UNMERGEABLE ]]; then
    if $DRY; then
      emit "$n" UNMERGEABLE "would dequeue (head $short)" none
      return 0
    fi
    # Marker first: the next pass reads this dequeue as reason `manual` and
    # must still know it was ours.
    local dq_id
    dq_id="$(gh api "repos/{owner}/{repo}/issues/$n/comments" -f body="Merge sweep: dequeued at ${short}: the queue entry was UNMERGEABLE (a merge-group check failed).
$dq_marker" --jq .id 2>/dev/null)"
    if [[ ! "$dq_id" =~ ^[0-9]+$ ]]; then
      failures=$((failures + 1))
      emit "$n" "DEQUEUE FAILED" "queue entry UNMERGEABLE (head $short), could not post the dequeue marker; not dequeued" "re-check later"
      return 0
    fi
    if ! dequeue "$id"; then
      failures=$((failures + 1))
      # Withdraw the marker: left behind, it would make a later hand dequeue read as ours.
      local left="dequeue marker removed"
      gh api -X DELETE "repos/{owner}/{repo}/issues/comments/$dq_id" >/dev/null 2>&1 \
        || left="dequeue marker comment $dq_id NOT removed (delete failed): delete it by hand"
      emit "$n" "DEQUEUE FAILED" "queue entry UNMERGEABLE (head $short), dequeuePullRequest failed; $left" "re-check later"
      return 0
    fi
    echo "#$n dequeued (was UNMERGEABLE, head $short)" >&2
    reason="sweep-dequeued"
  elif [[ -n "$qstate" ]]; then
    emit "$n" QUEUED "entry $qstate (head $short)" none
    return 0
  elif [[ "$auto" == yes ]]; then
    emit "$n" ARMED "auto-merge waiting for required checks (head $short)" none
    return 0
  elif [[ -z "$reason" ]]; then
    return 0
  fi

  local what="removed from the queue ($reason)"
  [[ "$reason" == sweep-dequeued ]] && what="dequeued by this sweep (UNMERGEABLE)"
  if [[ "$mergeable" == CONFLICTING || "$reason" == *conflict* ]]; then
    emit "$n" EJECTED "$what, head $short conflicts with main; not re-armed" "rebase on main"
    return 0
  fi
  local review st by
  if ! review="$(newest_review "$head")"; then
    failures=$((failures + 1))
    emit "$n" EJECTED "$what; cannot read the statuses of $short (API error); not re-armed" "re-check later"
    return 0
  fi
  if [[ "$review" == none ]]; then
    emit "$n" EJECTED "$what; head $short has no \"$CONTEXT\" status (pushed after the review?); not re-armed" "delta review"
    return 0
  fi
  read -r st by <<<"$review"
  if [[ "$st" != success ]]; then
    emit "$n" EJECTED "$what; newest \"$CONTEXT\" on $short is $st (by $by); not re-armed" "check review comments"
    return 0
  fi
  if [[ "$by" == *"[bot]" ]] || ! in_list "$by" "$APPROVERS"; then
    emit "$n" EJECTED "$what; \"$CONTEXT\" on $short is by $by, not an approver; not re-armed" "delta review"
    return 0
  fi
  if ! bodies="$(comments "$n")"; then
    failures=$((failures + 1))
    emit "$n" EJECTED "$what; cannot read the PR comments (API error); not re-armed" "re-check later"
    return 0
  fi
  if [[ "$reason" == manual ]]; then
    # Our own dequeue: the marker is posted right BEFORE dequeuePullRequest, so
    # it must sit between the previous removal and this one (a marker from an
    # older sweep dequeue never covers a later hand dequeue).
    if has_marker "$bodies" "$dq_marker" "$prev_removed_at" "$removed_at"; then
      what="dequeued by an earlier sweep (UNMERGEABLE)"
    else
      emit "$n" DEQUEUED "dequeued by hand (head $short); not re-armed" "re-arm with --arm if still wanted"
      return 0
    fi
  fi
  local marker="<!-- merge-sweep rearm $head -->"
  if has_marker "$bodies" "$marker"; then
    emit "$n" EJECTED "$what again; already re-armed once at $short; not re-armed" "fix the failing test"
    return 0
  fi
  if $DRY; then
    emit "$n" EJECTED "$what; would re-arm once at $short" none
    return 0
  fi
  if ! gh pr comment "$n" --body "Merge sweep: re-armed at ${short} after: ${what}. Once per head; a second ejection needs a fix.
$marker" >/dev/null 2>&1; then
    failures=$((failures + 1))
    emit "$n" EJECTED "$what; could not post the once-per-head marker; not re-armed" "re-check later"
    return 0
  fi
  local out rc=0
  out="$("$SAFE_MERGE" "$n" "$head" --rearm 2>/dev/null)" || rc=$?
  out="$(tail -n 1 <<<"$out")"
  case "$rc" in
    8) emit "$n" RE-ARMED "$what; $out" none ;;
    0) emit "$n" MERGED "$out" none ;;
    *) failures=$((failures + 1))
       emit "$n" "RE-ARM FAILED" "$what; safe-merge --rearm exit $rc: $out" "check review comments" ;;
  esac
}

# The `## Merge notes` lines of a body, HTML comments removed.
merge_notes() {
  tr -d '\r' | awk '
    /^## / { insec = ($0 ~ /^## Merge notes[[:space:]]*$/); next }
    !insec { next }
    { line = $0
      out = ""
      while (line != "") {
        if (incom) { i = index(line, "-->"); if (!i) { line = ""; break } line = substr(line, i + 3); incom = 0 }
        else { i = index(line, "<!--"); if (!i) { out = out line; line = ""; break }
               out = out substr(line, 1, i - 1); line = substr(line, i + 4); incom = 1 } }
      print out }'
}
# Issue numbers on the body's `Part of #N[, #M]` line(s), HTML comments removed.
part_of() {
  tr -d '\r' | awk '{ while ((i = index($0, "<!--"))) { j = index(substr($0, i), "-->"); if (!j) { $0 = substr($0, 1, i - 1); break }
                       $0 = substr($0, 1, i - 1) substr($0, i + j + 2) } print }' \
    | grep -oE '^[[:space:]]*Part of #[0-9]+([[:space:]]*,[[:space:]]*#[0-9]+)*' | grep -oE '#[0-9]+' | tr -d '#'
}

# Who closed an issue last: "PullRequest <n>", "Commit <oid>", another
# "<type> ..." or "" (no ClosedEvent); exit 1 on API error.
GQL_CLOSER='query($owner:String!,$repo:String!,$num:Int!){repository(owner:$owner,name:$repo){issue(number:$num){
  timelineItems(itemTypes:[CLOSED_EVENT], last:1){nodes{... on ClosedEvent{closer{__typename ... on PullRequest{number} ... on Commit{oid}}}}}}}}'
closed_by() {
  local res
  res="$(gh api graphql -F owner='{owner}' -F repo='{repo}' -F num="$1" -f query="$GQL_CLOSER" 2>/dev/null)" \
    && jq -e '.data.repository.issue.timelineItems.nodes' >/dev/null 2>&1 <<<"$res" || return 1
  jq -r '.data.repository.issue.timelineItems.nodes[-1].closer // empty
    | "\(.__typename) \(.number // .oid // "")"' <<<"$res"
}

sweep_merged() {  # one merged-PR JSON object
  local pr="$1" n author merged_at branch merge msha
  n="$(jq -r .number <<<"$pr")"
  selected "$n" || return 0
  author="$(jq -r '.author.login // ""' <<<"$pr")"
  in_list "$author" "$AUTHORS" || return 0
  merged_at="$(jq -r '.mergedAt // ""' <<<"$pr")"
  [[ -n "$merged_at" ]] && (( $(date -u -d "$merged_at" +%s 2>/dev/null || echo 0) >= cutoff )) || return 0
  branch="$(jq -r '.headRefName // ""' <<<"$pr")"
  merge="$(jq -r '.mergeCommit.oid // ""' <<<"$pr")"
  msha="${merge:0:8}"
  local targets
  targets=" $(jq -r '.body // ""' <<<"$pr" | part_of | tr '\n' ' ')"
  local done_notes=() line kind issue text info state ispr bodies check_issue=false
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*(-[[:space:]]+)?(close|note)[[:space:]]+#([0-9]+):[[:space:]]*(.+[^[:space:]])[[:space:]]*$ ]] || continue
    kind="${BASH_REMATCH[2]}" issue="${BASH_REMATCH[3]}" text="${BASH_REMATCH[4]//\{MERGE\}/$msha}"
    if [[ "$targets" != *" $issue "* ]]; then
      done_notes+=("#$issue refused (not on the Part of line)"); continue
    fi
    if ! info="$(gh api "repos/{owner}/{repo}/issues/$issue" --jq '"\(.state) \(.pull_request != null)"' 2>/dev/null)"; then
      failures=$((failures + 1)); done_notes+=("$kind #$issue: read failed"); continue
    fi
    read -r state ispr <<<"$info"
    if [[ "$ispr" != false ]]; then done_notes+=("#$issue refused (a pull request)"); continue; fi
    if ! bodies="$(comments "$issue")"; then
      failures=$((failures + 1)); done_notes+=("$kind #$issue: read failed"); continue
    fi
    local marker="<!-- merge-sweep $kind PR#$n -->"
    local body="$text

(#$n, merge $msha)
$marker"
    if [[ "$kind" == close ]]; then
      if has_marker "$bodies" "$marker"; then
        if [[ "$state" == open ]]; then
          done_notes+=("#$issue has the sweep's close comment but is open (reopened, or the close failed)"); check_issue=true
        else
          done_notes+=("#$issue already closed by the sweep")
        fi
        continue
      fi
      if [[ "$state" != open ]]; then
        # `close #N` is also a GitHub closing keyword, so the merge itself often
        # closed the issue (#2508): then post the delivery comment, once.
        local closer
        if ! closer="$(closed_by "$issue")"; then
          failures=$((failures + 1)); done_notes+=("close #$issue: read failed"); continue
        fi
        if [[ "$closer" != "PullRequest $n" && ( -z "$merge" || "$closer" != "Commit $merge" ) ]]; then
          done_notes+=("#$issue already closed"); continue
        fi
        if names_pr "$bodies" "$n" "$merged_at"; then
          done_notes+=("#$issue already has a delivery comment"); continue
        fi
        if $DRY; then done_notes+=("would comment #$issue (closed by this merge)"); continue; fi
        if gh issue comment "$issue" --body "$body" >/dev/null 2>&1; then done_notes+=("commented #$issue (closed by this merge)")
        else failures=$((failures + 1)); done_notes+=("comment #$issue FAILED"); fi
        continue
      fi
      if $DRY; then done_notes+=("would close #$issue"); continue; fi
      if gh issue close "$issue" --comment "$body" >/dev/null 2>&1; then done_notes+=("closed #$issue")
      else failures=$((failures + 1)); done_notes+=("close #$issue FAILED"); fi
    else
      if has_marker "$bodies" "$marker"; then done_notes+=("#$issue already noted"); continue; fi
      if $DRY; then done_notes+=("would note #$issue"); continue; fi
      if gh issue comment "$issue" --body "$body" >/dev/null 2>&1; then done_notes+=("noted #$issue")
      else failures=$((failures + 1)); done_notes+=("note #$issue FAILED"); fi
    fi
  done < <(jq -r '.body // ""' <<<"$pr" | merge_notes)
  local notes="no merge notes"
  if [[ ${#done_notes[@]} -gt 0 ]]; then
    local IFS=,; notes="merge notes: ${done_notes[*]}"
  fi
  local next=none
  [[ "$notes" == *FAILED* || "$notes" == *"read failed"* ]] && next="re-check later"
  [[ "$notes" == *refused* && "$next" == none ]] && next="check the merge notes"
  $check_issue && [[ "$next" == none ]] && next="check issue"
  emit "$n" MERGED "merge $msha, branch $branch; $notes" "$next"
}

while IFS= read -r pr; do sweep_open "$pr"; done < <(jq -c '.data.repository.pullRequests.nodes[]' <<<"$open_data")
# One line per merged PR even when two author searches overlap.
declare -A seen=()
while IFS= read -r pr; do
  [[ -n "$pr" ]] || continue
  num="$(jq -r .number <<<"$pr")"
  [[ -n "${seen[$num]:-}" ]] && continue
  seen[$num]=1
  sweep_merged "$pr"
done <<<"$merged_nodes"

echo "SWEEP: $lines PR line(s), $attention with a next step, $failures failed action(s)$($DRY && echo ' (dry run)')$($merged_ok || echo '; merged PRs not fully read')"
(( failures == 0 )) || exit 1
exit 0
