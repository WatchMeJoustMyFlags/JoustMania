#!/usr/bin/env bash
# Spec lint gate (#1263), run by the Spec Gate job in ci.yml and locally:
#
#   scripts/aiup/spec-lint-gate.sh
#
#  1. Every ERROR fails, baselined or not: the linter runs once WITHOUT the
#     baseline and any ERROR finding fails the gate.
#  2. Every WARN not in the committed baseline fails (--strict + baseline).
#  3. On pull requests (BASE_REF set): the baseline may not gain entries
#     compared with the base branch, unless the PR carries the
#     `spec-baseline-growth` label (read live from the API via PR_NUMBER).
#
# Env: BASE_REF (optional, e.g. main), PR_NUMBER + GH_TOKEN + GH_REPO (for the
# label lookup), PR_LABELS (optional comma list, local testing; skips the API).
set -euo pipefail

LINT=scripts/aiup/spec-review/scripts/spec_lint.py
BASELINE=scripts/aiup/spec-lint-baseline.json
GROWTH_LABEL="spec-baseline-growth"
HELP="docs/CONTRIBUTING.md#ci-spec-gate"
status=0

python3 -I "$LINT" --self-test

# 1. Errors always fail, whatever the baseline says.
errors=$(python3 -I "$LINT" --docs docs --no-baseline --format json || true)
error_count=$(jq '[.findings[] | select(.severity == "ERROR")] | length' <<<"$errors")
if [[ "$error_count" != "0" ]]; then
  echo "::error title=Spec lint errors::$error_count spec ERROR finding(s); errors cannot be baselined, fix them. See $HELP"
  jq -r '.findings[] | select(.severity == "ERROR") | "  \(.path):\(.line): \(.code) [\(.element)]: \(.message)"' <<<"$errors"
  status=1
fi

# 2. New warnings fail; baselined ones are suppressed (and counted).
echo "Accepted (baselined) findings by code:"
jq -r '.findings[].code' "$BASELINE" | sort | uniq -c
python3 -I "$LINT" --docs docs --strict --baseline "$BASELINE" || status=1

# 3. The baseline only shrinks, unless deliberately accepted.
if [[ -n "${BASE_REF:-}" ]]; then
  # Only a clone that is ALREADY shallow (the CI checkout) gets a depth fetch.
  # A depth fetch in a full clone makes it shallow, which breaks merge-bases
  # for every worktree sharing that clone, so local runs fetch normally.
  if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
    git fetch --no-tags --depth=1 origin "$BASE_REF" >/dev/null 2>&1
  else
    git fetch --no-tags origin "$BASE_REF" >/dev/null 2>&1
  fi
  if base_json=$(git show "FETCH_HEAD:$BASELINE" 2>/dev/null); then
    base_count=$(jq '.findings | length' <<<"$base_json")
    head_count=$(jq '.findings | length' "$BASELINE")
    echo "Baseline entries: $base_count on $BASE_REF, $head_count in this PR."
    if (( head_count > base_count )); then
      if [[ -n "${PR_LABELS+x}" ]]; then
        labels="$PR_LABELS"
      else
        labels="$(gh pr view "$PR_NUMBER" --json labels --jq '[.labels[].name] | join(",")')"
      fi
      if [[ ",$labels," == *",$GROWTH_LABEL,"* ]]; then
        echo "Baseline grows by $((head_count - base_count)); accepted by the '$GROWTH_LABEL' label."
      else
        echo "::error title=Spec lint baseline grew::$BASELINE grows from $base_count to $head_count entries. Fix the new findings, or add the '$GROWTH_LABEL' label to accept them deliberately. See $HELP"
        status=1
      fi
    fi
  else
    echo "No baseline on $BASE_REF yet; growth check skipped."
  fi
fi

exit "$status"
