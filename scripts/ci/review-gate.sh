#!/usr/bin/env bash
# review-gate.sh: the "Reviewed head" check in the merge queue (#1809).
#
#   HEAD_REF=gh-readonly-queue/main/pr-<N>-<base sha> review-gate.sh
#
# Run by .github/workflows/review-gate.yml on merge_group only. A merge-queue
# commit has its own SHA, so the reviewer's "Reviewed head" commit status on the
# PR head (posted by `safe-merge.sh --arm`) never reaches it; this job reports
# the same context name there instead:
#   1. PR number N from the queue branch name (HEAD_REF);
#   2. the PR's current head H (`pulls/N`);
#   3. the NEWEST "Reviewed head" status on H (highest status id; a later
#      `safe-merge.sh --revoke` failure status wins over an older success);
#   4. pass only when that status is `success` and its creator is in
#      REVIEW_APPROVERS (comma or space separated, default aepfli). A bot
#      creator (github-actions[bot] and any other `[bot]`) never passes.
# Fails CLOSED: an unparsable ref, an API error or an empty answer is a fail.
# An ejected PR can be re-armed; an unreviewed merge cannot be undone.
#
# Env: HEAD_REF (required), REVIEW_APPROVERS (default aepfli), GH_TOKEN / GH_REPO
#      for gh (read-only GITHUB_TOKEN in the workflow).
# Prints one line; exit 0 reviewed, 1 not reviewed or unknown.
set -uo pipefail

CONTEXT="Reviewed head"
fail() { echo "NOT REVIEWED: $*"; exit 1; }

ref="${HEAD_REF:-}"
ref="${ref#refs/heads/}"
[[ "$ref" =~ ^gh-readonly-queue/main/pr-([0-9]+)-[0-9a-f]{40}$ ]] \
  || fail "cannot parse a PR number from the merge-queue ref '${HEAD_REF:-}'"
pr="${BASH_REMATCH[1]}"

approvers="${REVIEW_APPROVERS:-}"
[[ -n "${approvers//[[:space:],]/}" ]] || approvers="aepfli"

head="$(gh api "repos/{owner}/{repo}/pulls/$pr" --jq .head.sha 2>/dev/null)" \
  || fail "cannot read PR #$pr (API error)"
[[ "$head" =~ ^[0-9a-f]{40}$ ]] || fail "PR #$pr has no readable head SHA ('$head')"

# id state creator, one line per "Reviewed head" status across all pages.
entries="$(gh api "repos/{owner}/{repo}/commits/$head/statuses?per_page=100" --paginate \
  --jq ".[] | select(.context == \"$CONTEXT\") | \"\(.id) \(.state) \(.creator.login // \"-\")\"" 2>/dev/null)" \
  || fail "cannot read the statuses of $head (API error)"
newest="$(sort -k1,1nr <<<"$entries" | sed -n '1p')"
[[ -n "$newest" ]] || fail "PR #$pr head ${head:0:8} has no \"$CONTEXT\" status"
read -r id state creator <<<"$newest"
[[ "$id" =~ ^[0-9]+$ ]] || fail "unparsable status entry '$newest'"

[[ "$state" == success ]] || fail "PR #$pr head ${head:0:8}: newest \"$CONTEXT\" is $state (by $creator)"
[[ "$creator" == *"[bot]" ]] && fail "PR #$pr head ${head:0:8}: \"$CONTEXT\" was posted by a bot ($creator)"
set -f  # no glob expansion of the approver list
for a in ${approvers//,/ }; do
  if [[ "$creator" == "$a" ]]; then
    echo "REVIEWED: PR #$pr head ${head:0:8} by $creator"
    exit 0
  fi
done
fail "PR #$pr head ${head:0:8}: \"$CONTEXT\" by $creator, who is not in REVIEW_APPROVERS ($approvers)"
