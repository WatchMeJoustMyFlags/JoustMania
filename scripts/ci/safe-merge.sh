#!/usr/bin/env bash
# safe-merge.sh: wait for a PR's CI gently, then squash-merge it at an exact head.
#
#   safe-merge.sh <PR> <HEAD_PREFIX> [options]
#
#   --wait-ci                 wait and report only; never merge, never touch issues
#   --rerun-failed-once       on red, rerun the failed jobs once and wait again
#                             (a cancelled run: rerun the whole run once)
#   --close N,N               close these issues after the merge ...
#   --comment-file F          ... with this comment ({MERGE} -> short merge SHA);
#                             optional: default "Done in #<PR>: <PR title>"
#   --note-issue N            comment on one more issue after the merge ...
#   --note-file F             ... with this body ({MERGE} substituted too)
#   --wait-queue              merge queue only: after enqueueing, wait until the
#                             queue merges the PR (then close/note issues as
#                             usual) or ejects it, instead of returning QUEUED
#   --arm                     #1809: record the review and arm native auto-merge,
#                             never wait (HEAD_PREFIX must be the full 40-hex SHA):
#                             post the "Reviewed head" success status on the SHA,
#                             then `gh pr merge --auto --squash --match-head-commit`.
#                             Refuses (exit 2, nothing posted) unless the `main`
#                             ruleset requires the "Reviewed head" check, and
#                             with --wait-ci, --wait-queue, --rerun-failed-once,
#                             --close / --comment-file and --note-*.
#   --review-url U            --arm / --revoke: target_url of the status (verdict)
#   --depth D                 --arm: review depth named in the status description
#   --revoke                  #1809: withdraw the review of the full SHA: post a
#                             "Reviewed head" failure status (the newest status
#                             wins). No merge, no PR change otherwise.
#
# Rules:
#   - Polls once every SAFE_MERGE_POLL seconds (default 75, never below 60
#     unless SAFE_MERGE_TEST=1); API errors and rate
#     limits back off (doubling, capped at 600 s). Never uses `gh run watch`
#     (its 3 s polling tripped GitHub's secondary rate limit).
#   - SonarCloud checks are ignored (informational, not merge-blocking).
#   - Per workflow, only checks of the NEWEST run on the head count (what GitHub
#     applies too, #1523): a cancelled run superseded by a newer push is ignored.
#   - Green means: nothing pending, nothing failing, at least one counted check,
#     and "CI Complete" SUCCESS in the chosen CI run. An empty rollup is waited on.
#   - Merges only when the PR head starts with HEAD_PREFIX, with
#     `gh pr merge --squash --match-head-commit <full sha>`. Right before that
#     it posts the "Reviewed head" success status on the head (#1809; the merge
#     still goes ahead if the post fails).
#   - --arm (#1809) checks the ruleset, then the PR once (MERGED -> already
#     merged, head mismatch -> 3, CONFLICTING -> 5), posts the status and arms
#     auto-merge whatever the CI state is: GitHub queues the PR when the required
#     checks pass. ARMED (exit 8); a failed state read after a successful arm
#     reads "ARMED (state unverified)".
#   - Issues are closed / commented ONLY after the PR state reads MERGED and
#     the merged head still starts with HEAD_PREFIX.
#   - A CONFLICTING PR fails fast (exit 5): GitHub runs no CI on it, so
#     waiting would only end in a timeout. It needs a rebase.
#   - A newest run that was cancelled before its matrix expanded is
#     infrastructure (infra=cancelled-run): nothing in it FAILED and a job
#     whose name is still an unexpanded `${{ ... }}` template is CANCELLED.
#     Template-named jobs alone prove nothing: every real run has SKIPPED
#     ones (e.g. `Publish ${{ matrix.service }}`); a human cancel mid-run or
#     a job timeout cancels expanded jobs only. --rerun-failed-once reruns
#     the whole run once with `gh run rerun <id>`. A cancelled run that a
#     newer run on the same head superseded is already ignored (rule above).
#   - Merge queue (#1555): when main requires a merge queue, `gh pr merge`
#     enqueues the PR instead of merging it, or, while GitHub still sees
#     required checks pending, enables auto-merge (the PR is enqueued later).
#     Either way the PR reads OPEN with isInMergeQueue=true or an
#     autoMergeRequest, and the result is QUEUED (exit 6); issues are NOT
#     closed. With --wait-queue the script polls (every SAFE_MERGE_POLL s, up
#     to SAFE_MERGE_QUEUE_WAIT s) until the PR reads MERGED (normal MERGED
#     path, issues closed) or leaves the queue unmerged: EJECTED (exit 7).
#     EJECTED needs a successful confirming read: up to 3 re-reads,
#     SAFE_MERGE_CONFIRM s apart; a failed read never counts as "not queued".
#     Still queued when the wait runs out: QUEUED.
#   - On red, the failed job logs are scanned for known infrastructure
#     failures (stale images, registry blobs) and the advice says rerun or
#     add the ci-full label.
#
# Prints ONE result line on stdout (progress goes to stderr).
# Exit: 0 merged (or green with --wait-ci), 1 red CI / merge failed,
#       2 usage, 3 head mismatch, 4 timeout, 5 CONFLICTING (needs rebase),
#       6 QUEUED (in the merge queue, not merged yet; no issue changes),
#       7 EJECTED (left the merge queue unmerged; no issue changes),
#       8 ARMED (--arm: review recorded, auto-merge enabled; nothing waited for).
#       --revoke: 0 revoked, 1 the status post failed.
#
# Env: SAFE_MERGE_POLL (75), SAFE_MERGE_MAX_WAIT (seconds, 7200),
#      SAFE_MERGE_QUEUE_WAIT (seconds to wait in the queue with --wait-queue, 3600),
#      SAFE_MERGE_SETTLE (seconds before re-reading the merged state, 5),
#      SAFE_MERGE_CONFIRM (seconds between the reads confirming an ejection, 10),
#      SAFE_MERGE_TEST (1 lifts the 60 s poll floor; tests only).
set -uo pipefail

usage() { sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }

[[ $# -ge 2 ]] || usage
PR="$1"; WANT="$2"; shift 2
[[ "$PR" =~ ^[0-9]+$ && "$WANT" =~ ^[0-9a-f]{7,40}$ ]] || usage
# --close takes 7 or 7,8 (digits and commas only); --note-issue one number.
bad_number() { echo "$1 takes issue numbers like 7 or 7,8 (--note-issue: one): '$2'" >&2; exit 2; }
WAIT_ONLY=false RERUN=false WAIT_QUEUE=false CLOSE="" CFILE="" NOTE_ISSUE="" NFILE=""
ARM=false REVOKE=false REVIEW_URL="" DEPTH=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait-ci) WAIT_ONLY=true ;;
    --rerun-failed-once) RERUN=true ;;
    --wait-queue) WAIT_QUEUE=true ;;
    --close) [[ $# -ge 2 && "$2" =~ ^[0-9]+(,[0-9]+)*$ ]] || bad_number --close "${2-}"; CLOSE="$2"; shift ;;
    --comment-file) [[ $# -ge 2 ]] || usage; CFILE="$2"; shift ;;
    --note-issue) [[ $# -ge 2 && "$2" =~ ^[0-9]+$ ]] || bad_number --note-issue "${2-}"; NOTE_ISSUE="$2"; shift ;;
    --note-file) [[ $# -ge 2 ]] || usage; NFILE="$2"; shift ;;
    --arm) ARM=true ;;
    --revoke) REVOKE=true ;;
    --review-url) [[ $# -ge 2 && "$2" =~ ^https://[^[:space:]]+$ ]] || { echo "--review-url takes an https:// URL: '${2-}'" >&2; exit 2; }
      REVIEW_URL="$2"; shift ;;
    --depth) [[ $# -ge 2 && "$2" =~ ^[a-z][a-z-]{0,19}$ ]] || { echo "--depth takes a word like full or delta: '${2-}'" >&2; exit 2; }
      DEPTH="$2"; shift ;;
    *) usage ;;
  esac
  shift
done
# All arguments are validated here, before any wait or merge step (#2036).
[[ -z "$CFILE" || -n "$CLOSE" ]] || { echo "--comment-file needs --close" >&2; exit 2; }
[[ -n "$NOTE_ISSUE" && -z "$NFILE" ]] && { echo "--note-issue needs --note-file" >&2; exit 2; }
[[ -n "$NFILE" && -z "$NOTE_ISSUE" ]] && { echo "--note-file needs --note-issue" >&2; exit 2; }
for f in "$CFILE" "$NFILE"; do [[ -z "$f" || -r "$f" ]] || { echo "cannot read $f" >&2; exit 2; }; done
# #1809: --arm / --revoke act on one exact SHA and never wait or touch issues.
if $ARM && $REVOKE; then echo "--arm and --revoke exclude each other" >&2; exit 2; fi
if $ARM || $REVOKE; then
  [[ "$WANT" =~ ^[0-9a-f]{40}$ ]] || { echo "--arm / --revoke need the full 40-hex head SHA, not '$WANT'" >&2; exit 2; }
  if $WAIT_ONLY || $WAIT_QUEUE || $RERUN || [[ -n "$CLOSE$CFILE$NOTE_ISSUE$NFILE" ]]; then
    echo "--arm / --revoke take no --wait-ci, --wait-queue, --rerun-failed-once, --close, --comment-file or --note-*" >&2; exit 2
  fi
fi
[[ -n "$DEPTH" ]] && ! $ARM && { echo "--depth needs --arm" >&2; exit 2; }
[[ -n "$REVIEW_URL" ]] && ! $ARM && ! $REVOKE && { echo "--review-url needs --arm or --revoke" >&2; exit 2; }

POLL="${SAFE_MERGE_POLL:-75}"
MAX_WAIT="${SAFE_MERGE_MAX_WAIT:-7200}"
SETTLE="${SAFE_MERGE_SETTLE:-5}"
QUEUE_WAIT="${SAFE_MERGE_QUEUE_WAIT:-3600}"
CONFIRM="${SAFE_MERGE_CONFIRM:-10}"
for v in "$POLL" "$MAX_WAIT" "$SETTLE" "$QUEUE_WAIT" "$CONFIRM"; do
  [[ "$v" =~ ^[0-9]+$ ]] || { echo "SAFE_MERGE_* values must be whole seconds (got '$v')" >&2; exit 2; }
done
# GitHub's secondary rate limit: never poll faster than once a minute for real.
if [[ "${SAFE_MERGE_TEST:-}" != 1 ]] && (( POLL < 60 )); then POLL=60; fi

log() { echo "[safe-merge #$PR] $*" >&2; }
done_line() { echo "#$PR $1"; exit "$2"; }

# jq: reduce the rollup to the checks that count, then classify them.
# Each CheckRun's run id comes from its detailsUrl (/actions/runs/<id>/job/...).
read -r -d '' JQ_CLASSIFY <<'JQ'
[ .statusCheckRollup[]
  | select(((.name // .context // "") | test("sonar"; "i")) | not)
  | . + { run: ((.detailsUrl // "") | (capture("/runs/(?<id>[0-9]+)").id // "0") | tonumber),
          wf: (.workflowName // .context // "status") } ]
| (group_by(.wf)
   | map(
       (map(.run) | max) as $newest
       | map(select(.run == $newest)))
   | add // []) as $live
| {
    counted: ($live | length),
    ci_complete: ([ $live[] | select(.__typename == "CheckRun" and .name == "CI Complete")
                    | if .status == "COMPLETED" then (.conclusion // "") else "PENDING" end ] | first // ""),
    pending: [ $live[] | select(
        (.__typename == "CheckRun" and .status != "COMPLETED") or
        (.__typename == "StatusContext" and (.state == "PENDING" or .state == "EXPECTED"))) | (.name // .context) ],
    failing: [ $live[] | select(
        (.__typename == "CheckRun" and .status == "COMPLETED" and
          ((.conclusion // "") | IN("FAILURE","TIMED_OUT","CANCELLED","ACTION_REQUIRED","STARTUP_FAILURE"))) or
        (.__typename == "StatusContext" and (.state == "FAILURE" or .state == "ERROR"))) | (.name // .context) ],
    failing_runs: [ $live[] | select(.__typename == "CheckRun" and .status == "COMPLETED" and
          ((.conclusion // "") | IN("FAILURE","TIMED_OUT","STARTUP_FAILURE"))) | .run ] | unique,
    # Cancelled before the matrix expanded: nothing red but CANCELLED, and a
    # CANCELLED job still carries an unexpanded `${{ ... }}` template name.
    # SKIPPED template jobs exist in every run and do not count; a human
    # cancel or a job timeout cancels expanded jobs only: not matched.
    cancelled_runs: [ $live | map(select(.__typename == "CheckRun" and .run != 0)) | group_by(.run)[]
          | select(all(.[]; ((.conclusion // "") | IN("FAILURE","TIMED_OUT","ACTION_REQUIRED","STARTUP_FAILURE")) | not)
                   and any(.[]; .status == "COMPLETED" and .conclusion == "CANCELLED"
                                and ((.name // "") | contains("${{"))))
          | .[0].run ]
  }
JQ
# Fetch PR state with backoff on API errors. Sets $VIEW.
fetch_view() {
  local delay="$POLL" i
  for i in 1 2 3 4 5 6; do
    if VIEW="$(gh pr view "$PR" --json headRefOid,state,mergeable,statusCheckRollup 2>/dev/null)" && [[ -n "$VIEW" ]]; then
      return 0
    fi
    log "gh pr view failed (attempt $i), backing off ${delay}s"
    sleep "$delay"
    delay=$(( delay * 2 > 600 ? 600 : delay * 2 ))
  done
  return 1
}

# Scan failed job logs for infrastructure signatures. Prints "kind|advice" or nothing.
diagnose() {
  local id logs
  for id in "$@"; do
    [[ "$id" == 0 ]] && continue
    logs="$(gh run view "$id" --log-failed 2>/dev/null)" || continue
    if grep -qE 'unknown type: "span_metrics"|otel-collector[^[:cntrl:]]*exited \(1\)|collector[^[:cntrl:]]*exited \(1\)' <<<"$logs"; then
      echo "stale-collector-image|add the ci-full label (collector image predates its config)"; return
    fi
    if grep -qiE 'stale prebuilt images|freshness guard FAILED|predates current deps fingerprint' <<<"$logs"; then
      echo "stale-prebuilt-images|add the ci-full label (fast-path guard: prebuilt images are stale)"; return
    fi
    if grep -qiE 'blob not found|blob unknown' <<<"$logs"; then
      echo "registry-blob-not-found|rerun the failed jobs (transient registry error)"; return
    fi
  done
}

# Merge queue state (gh pr view has no isInMergeQueue field: GraphQL).
# Sets qstate qhead qin qentry qsha qauto qurl; returns 1 when the read fails.
# qauto is the auto-merge enabledAt timestamp ("" when auto-merge is off).
read -r -d '' GQL_QUEUE <<'GQL'
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      state headRefOid isInMergeQueue url
      mergeQueueEntry { state }
      mergeCommit { oid }
      autoMergeRequest { enabledAt }
    }
  }
}
GQL
queue_read() {
  local line
  line="$(gh api graphql -F owner='{owner}' -F repo='{repo}' -F pr="$PR" -f query="$GQL_QUEUE" \
    --jq '.data.repository.pullRequest | [.state, .headRefOid, (.isInMergeQueue | tostring),
          (.mergeQueueEntry.state // "-"), (.mergeCommit.oid // "-"),
          (.autoMergeRequest.enabledAt // "-"), .url] | join(" ")' 2>/dev/null)" || return 1
  read -r qstate qhead qin qentry qsha qauto qurl <<<"$line"
  [[ -n "${qstate:-}" ]] || return 1
  [[ "$qentry" == - ]] && qentry=""
  [[ "$qsha" == - ]] && qsha=""
  [[ "$qauto" == - ]] && qauto=""
  return 0
}
# Waiting to merge: in the queue, or auto-merge armed (enqueued once checks pass).
queued() { [[ "${qin:-}" == true || -n "${qauto:-}" ]]; }
queue_desc() {
  if [[ "${qin:-}" == true ]]; then echo "entry ${qentry:-unknown}"; else echo "auto-merge enabled at ${qauto}"; fi
}

# Leaving the queue unmerged, confirmed: up to 3 re-reads CONFIRM s apart (the
# queue may drop the entry a while before the PR reads MERGED). Returns 0 when
# merged, 1 when queued again or no read succeeded (keep waiting); exits 7
# when a successful read shows the PR neither merged nor queued.
confirm_ejected() {
  local i
  for i in 1 2 3; do
    sleep "$CONFIRM"
    if ! queue_read; then log "confirming read $i failed"; continue; fi
    [[ "$qstate" == MERGED ]] && return 0
    [[ "$qstate" != OPEN ]] && done_line "EJECTED: PR is $qstate, not merged (head $short); no issue changes" 7
    queued && return 1
    (( i < 3 )) && continue
    done_line "EJECTED from the merge queue (head $short); not merged, no issue changes; check the merge_group run" 7
  done
  log "no confirming read succeeded; still waiting"
  return 1
}

# --wait-queue: poll until MERGED (returns), ejected (exit 7) or out of time (exit 6).
wait_queue() {
  local qstart; qstart=$(date +%s)
  while :; do
    log "waiting in the merge queue ($(queue_desc)); next poll in ${POLL}s"
    sleep "$POLL"
    if ! queue_read; then
      log "merge queue read failed; retrying"
    elif [[ "$qstate" == MERGED ]]; then
      return 0
    elif [[ "$qstate" != OPEN ]]; then
      done_line "EJECTED: PR is $qstate, not merged (head $short); no issue changes" 7
    elif ! queued; then
      confirm_ejected && return 0
    fi
    if (( $(date +%s) - qstart >= QUEUE_WAIT )); then
      done_line "QUEUED after ${QUEUE_WAIT}s wait (head $short, $(queue_desc)); not merged yet, no issue changes" 6
    fi
  done
}

# #1809: the "Reviewed head" commit status on one full SHA (newest status wins).
REVIEW_CONTEXT="Reviewed head"
post_review_status() {  # sha state description -> 0 posted
  local sha="$1"; shift
  local args=(-f state="$1" -f context="$REVIEW_CONTEXT" -f description="$2")
  [[ -n "$REVIEW_URL" ]] && args+=(-f target_url="$REVIEW_URL")
  gh api -X POST "repos/{owner}/{repo}/statuses/$sha" "${args[@]}" >/dev/null
}
# Arming is only safe while the ruleset makes the status required: otherwise
# auto-merge merges on CI Complete alone. A failed read counts as not required.
review_required() {
  local contexts
  contexts="$(gh api "repos/{owner}/{repo}/rules/branches/main" \
    --jq '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context' 2>/dev/null)" || return 1
  grep -qxF "$REVIEW_CONTEXT" <<<"$contexts"
}

if $REVOKE; then
  post_review_status "$WANT" failure "revoked ${WANT:0:8}" \
    || done_line "REVOKE FAILED: could not post the \"$REVIEW_CONTEXT\" failure status on ${WANT:0:8}" 1
  done_line "REVOKED review of ${WANT:0:8} (\"$REVIEW_CONTEXT\" = failure)" 0
fi

if $ARM; then
  review_required \
    || done_line "NOT ARMED: \"$REVIEW_CONTEXT\" is not enforced (not a required check in the main ruleset, or the rules read failed); nothing posted" 2
  fetch_view || done_line "ERROR: GitHub API unavailable after retries" 1
  head="$(jq -r .headRefOid <<<"$VIEW")"
  state="$(jq -r .state <<<"$VIEW")"
  short="${head:0:8}"
  [[ "$state" == MERGED ]] && done_line "already MERGED (head $short); nothing done" 0
  [[ "$state" != OPEN ]] && done_line "NOT ARMED: PR is $state" 1
  [[ "$head" == "$WANT" ]] || done_line "NOT ARMED: head $short does not match ${WANT:0:8}" 3
  if [[ "$(jq -r '.mergeable // ""' <<<"$VIEW")" == CONFLICTING ]]; then
    done_line "NOT ARMED: CONFLICTING (head $short); needs a rebase on main" 5
  fi
  # Posted before arming on purpose: a failed arm leaves a reviewed SHA, harmless.
  post_review_status "$head" success "review-pr${DEPTH:+ $DEPTH} $short" \
    || done_line "NOT ARMED: could not post the \"$REVIEW_CONTEXT\" status on $short" 1
  log "review recorded on $head; arming auto-merge"
  merge_rc=0
  gh pr merge "$PR" --auto --squash --match-head-commit "$head" >&2 || merge_rc=$?
  if ! queue_read; then
    (( merge_rc == 0 )) && done_line "ARMED (head $short, state unverified); reviewed, auto-merge requested" 8
    done_line "ARM FAILED (head $short): gh pr merge --auto returned $merge_rc; reviewed status stays" 1
  fi
  [[ "$qstate" == MERGED ]] && done_line "MERGED by auto-merge (head $short); no issue changes" 0
  if [[ "$qstate" == OPEN && "$qhead" == "$head" ]] && queued; then
    if [[ "$qin" == true ]]; then done_line "ARMED (head $short, queued: $(queue_desc))" 8; fi
    done_line "ARMED (head $short, auto-merge: $(queue_desc))" 8
  fi
  done_line "ARM FAILED (head $short): PR reads $qstate, not armed (gh pr merge --auto returned $merge_rc); reviewed status stays" 1
fi

start=$(date +%s)
reran=false
while :; do
  fetch_view || done_line "ERROR: GitHub API unavailable after retries" 1
  head="$(jq -r .headRefOid <<<"$VIEW")"
  state="$(jq -r .state <<<"$VIEW")"
  short="${head:0:8}"
  [[ "$state" == MERGED ]] && done_line "already MERGED (head $short); nothing done" 0
  [[ "$state" != OPEN ]] && done_line "NOT MERGED: PR is $state" 1
  if [[ "$head" != "$WANT"* ]]; then
    done_line "NOT MERGED: head $short does not match $WANT" 3
  fi
  if [[ "$(jq -r '.mergeable // ""' <<<"$VIEW")" == CONFLICTING ]]; then
    done_line "NOT MERGED: CONFLICTING (head $short); needs a rebase on main" 5
  fi

  cls="$(jq -c "$JQ_CLASSIFY" <<<"$VIEW")"
  n_pending="$(jq '.pending | length' <<<"$cls")"
  n_failing="$(jq '.failing | length' <<<"$cls")"
  n_counted="$(jq '.counted' <<<"$cls")"
  ci_complete="$(jq -r '.ci_complete' <<<"$cls")"

  if [[ "$n_pending" == 0 && "$n_failing" != 0 ]]; then
    mapfile -t runs < <(jq -r '.failing_runs[]' <<<"$cls")
    mapfile -t cancelled < <(jq -r '.cancelled_runs[]' <<<"$cls")
    if $RERUN && ! $reran && [[ ${#runs[@]} -gt 0 || ${#cancelled[@]} -gt 0 ]]; then
      for id in "${cancelled[@]}"; do log "rerunning cancelled run $id"; gh run rerun "$id" >&2 || true; done
      for id in "${runs[@]}"; do log "rerunning failed jobs of run $id"; gh run rerun "$id" --failed >&2 || true; done
      reran=true
      sleep "$POLL"; continue
    fi
    failing="$(jq -r '.failing | join(", ")' <<<"$cls")"
    if [[ ${#cancelled[@]} -gt 0 && ${#runs[@]} -eq 0 ]]; then
      done_line "CI RED (head $short): [$failing] infra=cancelled-run; advice: rerun the whole run (gh run rerun ${cancelled[*]})" 1
    fi
    diag="$(diagnose "${runs[@]}")"
    if [[ -n "$diag" ]]; then
      done_line "CI RED (head $short): [$failing] infra=${diag%%|*}; advice: ${diag#*|}" 1
    fi
    done_line "CI RED (head $short): [$failing]; not infrastructure, needs a fix" 1
  fi

  if [[ "$n_pending" == 0 && "$n_counted" != 0 ]]; then
    [[ "$ci_complete" == SUCCESS ]] && break
    if [[ -n "$ci_complete" ]]; then
      done_line "CI NOT GREEN (head $short): CI Complete is $ci_complete" 1
    fi
  fi
  if (( $(date +%s) - start >= MAX_WAIT )); then
    done_line "TIMEOUT after ${MAX_WAIT}s (head $short): pending [$(jq -r '.pending | join(", ")' <<<"$cls")], $n_counted counted, CI Complete '${ci_complete:-absent}'" 4
  fi
  log "$n_counted counted, $n_pending pending, $n_failing failing, CI Complete '${ci_complete:-absent}'; next poll in ${POLL}s"
  sleep "$POLL"
done

if $WAIT_ONLY; then
  done_line "CI GREEN (head $short)$($reran && echo ' after one rerun'); not merged (--wait-ci)" 0
fi

post_review_status "$head" success "safe-merge ${head:0:8}" || log "could not post the \"$REVIEW_CONTEXT\" status; merging anyway"
log "merging at $head"
gh pr merge "$PR" --squash --match-head-commit "$head" >&2 || log "gh pr merge returned non-zero"
sleep "$SETTLE"
read -r mstate mhead murl msha < <(gh pr view "$PR" --json state,headRefOid,url,mergeCommit \
  --jq '[.state, .headRefOid, .url, (.mergeCommit.oid // "")] | join(" ")' 2>/dev/null || true)
if [[ "${mstate:-}" != MERGED ]]; then
  queue_read || done_line "MERGE FAILED (state ${mstate:-unknown}, head $short); no issue changes" 1
  if [[ "$qstate" == MERGED ]]; then
    mstate="$qstate" mhead="$qhead" murl="$qurl" msha="$qsha"
  elif ! queued; then
    done_line "MERGE FAILED (state ${mstate:-unknown}, head $short); no issue changes" 1
  elif ! $WAIT_QUEUE; then
    done_line "QUEUED in the merge queue (head $short, $(queue_desc)); not merged yet, no issue changes (--wait-queue waits for the result)" 6
  else
    wait_queue
    mstate="$qstate" mhead="$qhead" murl="$qurl" msha="$qsha"
  fi
fi
# Merged, possibly by someone else (gh pr merge may have failed): the merged
# head must still be the one we were asked to merge.
if [[ "${mhead:-}" != "$WANT"* ]]; then
  done_line "MERGED at head ${mhead:0:8}, not $WANT (merged elsewhere?); no issue changes" 3
fi
mshort="${msha:0:8}"
merge_ref="${mshort:-${murl:-#$PR}}"

substitute() { local body; body="$(<"$1")"; printf '%s' "${body//\{MERGE\}/"$merge_ref"}"; }

actions=""
if [[ -n "$CLOSE" ]]; then
  if [[ -n "$CFILE" ]]; then
    body="$(substitute "$CFILE")"
  else
    pr_title="$(gh pr view "$PR" --json title --jq .title 2>/dev/null || true)"
    body="Done in #$PR${pr_title:+: $pr_title}"
  fi
  IFS=, read -r -a close_list <<<"$CLOSE"
  for n in "${close_list[@]}"; do
    if gh issue close "$n" --comment "$body" >&2; then actions+=" closed #$n;"; else actions+=" close #$n FAILED;"; fi
  done
fi
if [[ -n "$NOTE_ISSUE" ]]; then
  body="$(substitute "$NFILE")"
  if gh issue comment "$NOTE_ISSUE" --body "$body" >&2; then actions+=" noted #$NOTE_ISSUE;"; else actions+=" note #$NOTE_ISSUE FAILED;"; fi
fi
done_line "MERGED as $merge_ref (head $short)$($reran && echo ', after one rerun');${actions%;}" 0
