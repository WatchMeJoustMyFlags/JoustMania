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
#      - queue entry UNMERGEABLE: dequeue it (GraphQL dequeuePullRequest), then
#        treat it as ejected (below);
#      - queued (any other entry state) or auto-merge armed: report, no action;
#      - not queued, not armed, with a RemovedFromMergeQueueEvent (dequeued or
#        ejected): re-arm with `safe-merge.sh <PR> <head> --arm --depth rearm`
#        only when ALL hold:
#          * not CONFLICTING and the last removal reason is not a conflict;
#          * the NEWEST "Reviewed head" status on the current head is success,
#            posted by a login in REVIEW_APPROVERS (never a `[bot]`);
#          * the last removal was not a hand dequeue (reason `manual`), unless
#            this pass made it;
#          * no re-arm marker for this head yet: at most ONE re-arm per head.
#            The marker is a PR comment `<!-- merge-sweep rearm <sha> -->`,
#            posted BEFORE arming (a crash never allows a second re-arm).
#        Otherwise it reports why and names the next step.
#      - never queued (no removal event): not part of the flow, no line.
#   2. Our PRs merged in the last SWEEP_MERGED_HOURS: a `MERGED` line with the
#      branch (the caller removes its worktree), then the PR body's
#      `## Merge notes` lines (HTML comments ignored):
#        close #N: <text>   close issue N with <text> (once: skipped when closed)
#        note #N: <text>    comment <text> on issue N (once per PR: marker)
#      `{MERGE}` in <text> becomes the short merge SHA.
# Fails CLOSED: an unreadable status or marker list never re-arms.
#
# --dry-run: read everything, change nothing (lines say "would ...").
# PR ...:    restrict the pass to these numbers (still only ours).
#
# Output: one line per PR, `#<n> <STATE>: <detail>; next: <step>`, then a
# `SWEEP:` summary. Exit 0 pass completed (lines may still name a next step),
# 1 the PR list could not be read or an action failed, 2 usage.
#
# Env: SWEEP_AUTHORS (default aepfli), REVIEW_APPROVERS (default aepfli),
#      SWEEP_MERGED_HOURS (default 48), SWEEP_SAFE_MERGE (default the sibling
#      safe-merge.sh; tests only), GH_REPO for gh.
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

AUTHORS="${SWEEP_AUTHORS:-}"
[[ -n "${AUTHORS//[[:space:],]/}" ]] || AUTHORS="aepfli"
APPROVERS="${REVIEW_APPROVERS:-}"
[[ -n "${APPROVERS//[[:space:],]/}" ]] || APPROVERS="aepfli"
HOURS="${SWEEP_MERGED_HOURS:-48}"
[[ "$HOURS" =~ ^[0-9]+$ ]] || { echo "SWEEP_MERGED_HOURS must be a number: '$HOURS'" >&2; exit 2; }
SAFE_MERGE="${SWEEP_SAFE_MERGE:-$(cd "$(dirname "$0")" && pwd)/safe-merge.sh}"

failures=0 attention=0 lines=0
emit() {  # number state detail next
  lines=$((lines + 1))
  [[ "$4" != none ]] && attention=$((attention + 1))
  echo "#$1 $2: $3; next: $4"
}

in_list() {  # word list(comma/space separated)
  local w
  set -f
  for w in ${2//,/ }; do [[ "$1" == "$w" ]] && { set +f; return 0; }; done
  set +f
  return 1
}
selected() {
  [[ ${#ONLY[@]} -eq 0 ]] && return 0
  local n
  for n in "${ONLY[@]}"; do [[ "$n" == "$1" ]] && return 0; done
  return 1
}

GQL='query($owner:String!,$repo:String!){repository(owner:$owner,name:$repo){
  open: pullRequests(states:OPEN, first:100){nodes{number id author{login} headRefOid mergeable
    autoMergeRequest{enabledAt} mergeQueueEntry{state}
    timelineItems(last:1, itemTypes:[REMOVED_FROM_MERGE_QUEUE_EVENT]){nodes{... on RemovedFromMergeQueueEvent{reason}}}}}
  merged: pullRequests(states:MERGED, first:30, orderBy:{field:UPDATED_AT, direction:DESC}){nodes{number author{login}
    headRefName mergedAt mergeCommit{oid} body}}}}'

data="$(gh api graphql -F owner='{owner}' -F repo='{repo}' -f query="$GQL" 2>/dev/null)" \
  && jq -e '.data.repository.open.nodes and .data.repository.merged.nodes' >/dev/null 2>&1 <<<"$data" \
  || { echo "SWEEP ERROR: cannot read the open and merged PRs (API error); nothing done"; exit 1; }

# Newest "Reviewed head" status on a SHA: "<state> <creator>", "none", or exit 1 (API error).
newest_review() {
  local entries newest
  entries="$(gh api "repos/{owner}/{repo}/commits/$1/statuses?per_page=100" --paginate \
    --jq ".[] | select(.context == \"$CONTEXT\") | \"\(.id) \(.state) \(.creator.login // \"-\")\"" 2>/dev/null)" || return 1
  newest="$(sort -k1,1nr <<<"$entries" | sed -n '1p')"
  if [[ -z "$newest" ]]; then echo none; return 0; fi
  read -r _ st by <<<"$newest"
  echo "$st $by"
}

# All comment bodies of an issue or PR (exit 1 on API error).
comments() { gh api "repos/{owner}/{repo}/issues/$1/comments?per_page=100" --paginate --jq '.[].body' 2>/dev/null; }

dequeue() {  # PR node id
  gh api graphql -f query='mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}' \
    -f id="$1" >/dev/null 2>&1
}

sweep_open() {  # one open-PR JSON object
  local pr="$1" n id author head mergeable auto qstate reason
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
  local short="${head:0:8}"

  if [[ "$qstate" == UNMERGEABLE ]]; then
    if $DRY; then
      emit "$n" UNMERGEABLE "would dequeue (head $short)" "none"
      return 0
    fi
    if ! dequeue "$id"; then
      failures=$((failures + 1))
      emit "$n" "DEQUEUE FAILED" "queue entry UNMERGEABLE (head $short), dequeuePullRequest failed" "re-check later"
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
  if [[ "$reason" == manual ]]; then
    emit "$n" DEQUEUED "dequeued by hand (head $short); not re-armed" "re-arm with --arm if still wanted"
    return 0
  fi
  local marker="<!-- merge-sweep rearm $head -->" bodies
  if ! bodies="$(comments "$n")"; then
    failures=$((failures + 1))
    emit "$n" EJECTED "$what; cannot read the PR comments (API error); not re-armed" "re-check later"
    return 0
  fi
  if grep -qF "$marker" <<<"$bodies"; then
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
  out="$("$SAFE_MERGE" "$n" "$head" --arm --depth rearm 2>/dev/null)" || rc=$?
  out="$(tail -n 1 <<<"$out")"
  case "$rc" in
    8) emit "$n" RE-ARMED "$what; $out" none ;;
    0) emit "$n" MERGED "$out" none ;;
    *) failures=$((failures + 1))
       emit "$n" "RE-ARM FAILED" "$what; safe-merge --arm exit $rc: $out" "re-brief" ;;
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

sweep_merged() {  # one merged-PR JSON object
  local pr="$1" n author merged_at branch merge msha cutoff
  n="$(jq -r .number <<<"$pr")"
  selected "$n" || return 0
  author="$(jq -r '.author.login // ""' <<<"$pr")"
  in_list "$author" "$AUTHORS" || return 0
  merged_at="$(jq -r '.mergedAt // ""' <<<"$pr")"
  cutoff=$(( $(date -u +%s) - HOURS * 3600 ))
  [[ -n "$merged_at" ]] && (( $(date -u -d "$merged_at" +%s 2>/dev/null || echo 0) >= cutoff )) || return 0
  branch="$(jq -r '.headRefName // ""' <<<"$pr")"
  merge="$(jq -r '.mergeCommit.oid // ""' <<<"$pr")"
  msha="${merge:0:8}"
  local done_notes=() line kind issue text
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*(-[[:space:]]+)?(close|note)[[:space:]]+#([0-9]+):[[:space:]]*(.+[^[:space:]])[[:space:]]*$ ]] || continue
    kind="${BASH_REMATCH[2]}" issue="${BASH_REMATCH[3]}" text="${BASH_REMATCH[4]//\{MERGE\}/$msha}"
    local marker="<!-- merge-sweep $kind PR#$n -->"
    local body="$text

(#$n, merge $msha)
$marker"
    if [[ "$kind" == close ]]; then
      local state
      if ! state="$(gh issue view "$issue" --json state --jq .state 2>/dev/null)"; then
        failures=$((failures + 1)); done_notes+=("close #$issue: read failed"); continue
      fi
      if [[ "$state" != OPEN ]]; then done_notes+=("#$issue already closed"); continue; fi
      if $DRY; then done_notes+=("would close #$issue"); continue; fi
      if gh issue close "$issue" --comment "$body" >/dev/null 2>&1; then done_notes+=("closed #$issue")
      else failures=$((failures + 1)); done_notes+=("close #$issue FAILED"); fi
    else
      local bodies
      if ! bodies="$(comments "$issue")"; then
        failures=$((failures + 1)); done_notes+=("note #$issue: read failed"); continue
      fi
      if grep -qF "$marker" <<<"$bodies"; then done_notes+=("#$issue already noted"); continue; fi
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
  emit "$n" MERGED "merge $msha, branch $branch; $notes" "$next"
}

while IFS= read -r pr; do sweep_open "$pr"; done < <(jq -c '.data.repository.open.nodes[]' <<<"$data")
while IFS= read -r pr; do sweep_merged "$pr"; done < <(jq -c '.data.repository.merged.nodes[]' <<<"$data")

echo "SWEEP: $lines PR line(s), $attention with a next step, $failures failed action(s)$($DRY && echo ' (dry run)')"
(( failures == 0 )) || exit 1
exit 0
