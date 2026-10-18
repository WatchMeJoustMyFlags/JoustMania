#!/usr/bin/env bash
# safe-merge.sh: wait for a PR's CI gently, then squash-merge it at an exact head.
#
#   safe-merge.sh <PR> <HEAD_PREFIX> [options]
#
#   --wait-ci                 wait and report only; never merge, never touch issues
#   --rerun-failed-once       on red, rerun the failed jobs once and wait again
#   --close N,N               close these issues after the merge ...
#   --comment-file F          ... with this comment ({MERGE} -> short merge SHA)
#   --note-issue N            comment on one more issue after the merge ...
#   --note-file F             ... with this body ({MERGE} substituted too)
#
# Rules:
#   - Polls once every SAFE_MERGE_POLL seconds (default 75); API errors and rate
#     limits back off (doubling, capped at 600 s). Never uses `gh run watch`
#     (its 3 s polling tripped GitHub's secondary rate limit).
#   - SonarCloud checks are ignored (informational, not merge-blocking).
#   - Per workflow, only checks of the NEWEST run on the head count: a cancelled
#     duplicate run (the opened+labeled double trigger) whose "CI Complete" job
#     reports FAILURE is superseded and ignored.
#   - Merges only when the PR head starts with HEAD_PREFIX, with
#     `gh pr merge --squash --match-head-commit <full sha>`.
#   - Issues are closed / commented ONLY after the PR state reads MERGED.
#   - On red, the failed job logs are scanned for known infrastructure
#     failures (stale images, registry blobs) and the advice says rerun or
#     add the ci-full label.
#
# Prints ONE result line on stdout (progress goes to stderr).
# Exit: 0 merged (or green with --wait-ci), 1 red CI / merge failed,
#       2 usage, 3 head mismatch, 4 timeout.
#
# Env: SAFE_MERGE_POLL (75), SAFE_MERGE_MAX_WAIT (seconds, 7200),
#      SAFE_MERGE_SETTLE (seconds before re-reading the merged state, 5).
set -uo pipefail

usage() { sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//' >&2; exit 2; }

[[ $# -ge 2 ]] || usage
PR="$1"; WANT="$2"; shift 2
[[ "$PR" =~ ^[0-9]+$ && "$WANT" =~ ^[0-9a-f]{7,40}$ ]] || usage
WAIT_ONLY=false RERUN=false CLOSE="" CFILE="" NOTE_ISSUE="" NFILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait-ci) WAIT_ONLY=true ;;
    --rerun-failed-once) RERUN=true ;;
    --close) CLOSE="${2:?}"; shift ;;
    --comment-file) CFILE="${2:?}"; shift ;;
    --note-issue) NOTE_ISSUE="${2:?}"; shift ;;
    --note-file) NFILE="${2:?}"; shift ;;
    *) usage ;;
  esac
  shift
done
[[ -n "$CLOSE" && -z "$CFILE" ]] && { echo "--close needs --comment-file" >&2; exit 2; }
[[ -n "$NOTE_ISSUE" && -z "$NFILE" ]] && { echo "--note-issue needs --note-file" >&2; exit 2; }
for f in "$CFILE" "$NFILE"; do [[ -z "$f" || -r "$f" ]] || { echo "cannot read $f" >&2; exit 2; }; done

POLL="${SAFE_MERGE_POLL:-75}"
MAX_WAIT="${SAFE_MERGE_MAX_WAIT:-7200}"
SETTLE="${SAFE_MERGE_SETTLE:-5}"

log() { echo "[safe-merge #$PR] $*" >&2; }
done_line() { echo "#$PR $1"; exit "$2"; }

# jq: reduce the rollup to the checks that count, then classify them.
# Each CheckRun's run id comes from its detailsUrl (/actions/runs/<id>/job/...).
read -r -d '' JQ_CLASSIFY <<'JQ'
[ .statusCheckRollup[]
  | select(((.name // .context // "") | test("sonar"; "i")) | not)
  | . + { run: ((.detailsUrl // "") | (capture("/runs/(?<id>[0-9]+)").id // "0") | tonumber),
          wf: (.workflowName // .context // "status") } ]
| (group_by(.wf) | map(max_by(.run).run as $newest | map(select(.run == $newest))) | add // []) as $live
| {
    pending: [ $live[] | select(
        (.__typename == "CheckRun" and .status != "COMPLETED") or
        (.__typename == "StatusContext" and (.state == "PENDING" or .state == "EXPECTED"))) | (.name // .context) ],
    failing: [ $live[] | select(
        (.__typename == "CheckRun" and .status == "COMPLETED" and
          ((.conclusion // "") | IN("FAILURE","TIMED_OUT","CANCELLED","ACTION_REQUIRED","STARTUP_FAILURE"))) or
        (.__typename == "StatusContext" and (.state == "FAILURE" or .state == "ERROR"))) | (.name // .context) ],
    failing_runs: [ $live[] | select(.__typename == "CheckRun" and .status == "COMPLETED" and
          ((.conclusion // "") | IN("FAILURE","TIMED_OUT","STARTUP_FAILURE"))) | .run ] | unique
  }
JQ

# Fetch PR state with backoff on API errors. Sets $VIEW.
fetch_view() {
  local delay="$POLL" i
  for i in 1 2 3 4 5 6; do
    if VIEW="$(gh pr view "$PR" --json headRefOid,state,statusCheckRollup 2>/dev/null)" && [[ -n "$VIEW" ]]; then
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

  cls="$(jq -c "$JQ_CLASSIFY" <<<"$VIEW")"
  n_pending="$(jq '.pending | length' <<<"$cls")"
  n_failing="$(jq '.failing | length' <<<"$cls")"

  if [[ "$n_pending" == 0 && "$n_failing" != 0 ]]; then
    mapfile -t runs < <(jq -r '.failing_runs[]' <<<"$cls")
    if $RERUN && ! $reran && [[ ${#runs[@]} -gt 0 ]]; then
      for id in "${runs[@]}"; do log "rerunning failed jobs of run $id"; gh run rerun "$id" --failed >&2 || true; done
      reran=true
      sleep "$POLL"; continue
    fi
    failing="$(jq -r '.failing | join(", ")' <<<"$cls")"
    diag="$(diagnose "${runs[@]}")"
    if [[ -n "$diag" ]]; then
      done_line "CI RED (head $short): [$failing] infra=${diag%%|*}; advice: ${diag#*|}" 1
    fi
    done_line "CI RED (head $short): [$failing]; not infrastructure, needs a fix" 1
  fi

  if [[ "$n_pending" == 0 ]]; then break; fi
  if (( $(date +%s) - start >= MAX_WAIT )); then
    done_line "TIMEOUT after ${MAX_WAIT}s (head $short): pending [$(jq -r '.pending | join(", ")' <<<"$cls")]" 4
  fi
  log "$n_pending pending, $n_failing failing; next poll in ${POLL}s"
  sleep "$POLL"
done

if $WAIT_ONLY; then
  done_line "CI GREEN (head $short)$($reran && echo ' after one rerun'); not merged (--wait-ci)" 0
fi

log "merging at $head"
gh pr merge "$PR" --squash --match-head-commit "$head" >&2 || log "gh pr merge returned non-zero"
sleep "$SETTLE"
merged="$(gh pr view "$PR" --json state,mergeCommit --jq '[.state, (.mergeCommit.oid // "")] | join(" ")' 2>/dev/null)"
mstate="${merged%% *}"; msha="${merged#* }"
if [[ "$mstate" != MERGED ]]; then
  done_line "MERGE FAILED (state ${mstate:-unknown}, head $short); no issue changes" 1
fi
mshort="${msha:0:8}"

actions=""
if [[ -n "$CLOSE" ]]; then
  body="$(sed "s/{MERGE}/$mshort/g" "$CFILE")"
  for n in ${CLOSE//,/ }; do
    if gh issue close "$n" --comment "$body" >&2; then actions+=" closed #$n;"; else actions+=" close #$n FAILED;"; fi
  done
fi
if [[ -n "$NOTE_ISSUE" ]]; then
  body="$(sed "s/{MERGE}/$mshort/g" "$NFILE")"
  if gh issue comment "$NOTE_ISSUE" --body "$body" >&2; then actions+=" noted #$NOTE_ISSUE;"; else actions+=" note #$NOTE_ISSUE FAILED;"; fi
fi
done_line "MERGED as $mshort (head $short)$($reran && echo ', after one rerun');${actions%;}" 0
