#!/usr/bin/env bash
# Spec-touched gate (#1263): a PR that changes service behaviour (services/**,
# lib/**, proto/**) must also change the spec, or carry the `no-spec-change`
# label, or be a bot / agent-promoter PR.
#
# Inputs (env):
#   PR_NUMBER            PR number: the changed files and the labels are read
#                        LIVE from the API (needs GH_TOKEN + GH_REPO), so a re-run
#                        after adding the label sees it (a re-run keeps the event
#                        payload, and with it the labels, of the original event)
#   PR_AUTHOR, PR_AUTHOR_TYPE, PR_TITLE, PR_HEAD_REF   exemption inputs
#   CHANGED_FILES_FILE   optional: newline list of changed files (local testing)
#   PR_LABELS            optional: comma-separated labels (local testing; skips
#                        the API call)
set -euo pipefail

LABEL="no-spec-change"
HELP="docs/CONTRIBUTING.md#spec-driven-development-aiup"

if [[ -n "${CHANGED_FILES_FILE:-}" ]]; then
  mapfile -t files < "$CHANGED_FILES_FILE"
else
  # Via a temp file, not process substitution, so an API failure fails the step.
  changed="$(mktemp)"
  gh api --paginate "repos/{owner}/{repo}/pulls/${PR_NUMBER}/files" \
    --jq '.[] | .filename, (.previous_filename // empty)' > "$changed"
  mapfile -t files < "$changed"
fi

# Files whose change can alter observable behaviour. Tests, docs, dependency
# manifests/locks and Dockerfiles are not behaviour by themselves.
is_behaviour_file() {
  local f="$1"
  case "$f" in
    services/*|lib/*|proto/*) ;;
    *) return 1 ;;
  esac
  case "$f" in
    */tests/*|*/test/*|*/testdata/*|*_test.go|*/test_*.py|*_test.py|*.test.ts|*.test.tsx|*.spec.ts) return 1 ;;
    *.md) return 1 ;;
    */pyproject.toml|*/uv.lock|*/go.mod|*/go.sum|*/package.json|*/package-lock.json|*/Cargo.toml|*/Cargo.lock|*/Dockerfile) return 1 ;;
    proto/*) [[ "$f" == *.proto ]] || return 1 ;;
  esac
  return 0
}

is_spec_file() {
  case "$1" in
    docs/requirements.md|docs/use_cases.puml|docs/use_cases/*|docs/specs/*|docs/specs-inventory.md|docs/entity_model.md) return 0 ;;
  esac
  return 1
}

behaviour=() flagd=() protos=()
spec_touched=false flags_spec=false services_spec=false
for f in "${files[@]}"; do
  [[ -z "$f" ]] && continue
  if is_behaviour_file "$f"; then
    behaviour+=("$f")
    [[ "$f" == services/flagd/*.json ]] && flagd+=("$f")
    [[ "$f" == proto/*.proto ]] && protos+=("$f")
  fi
  if is_spec_file "$f"; then spec_touched=true; fi
  [[ "$f" == docs/specs/flags.md ]] && flags_spec=true
  [[ "$f" == docs/specs/services.md ]] && services_spec=true
done

if [[ ${#behaviour[@]} -eq 0 ]]; then
  echo "No behaviour files changed (services/**, lib/**, proto/**): spec gate not applicable."
  exit 0
fi

# Rule 1: some spec file changed. Rule 2 (cheaper, more specific): flag
# definitions need the flag catalog, .proto changes need the services spec.
problems=()
if [[ "$spec_touched" != true ]]; then
  problems+=("Behaviour files changed but no spec file did (docs/requirements.md, docs/use_cases/**, docs/use_cases.puml, docs/specs/**, docs/specs-inventory.md, docs/entity_model.md).")
fi
if [[ ${#flagd[@]} -gt 0 && "$flags_spec" != true ]]; then
  problems+=("services/flagd/*.json changed but docs/specs/flags.md did not.")
fi
if [[ ${#protos[@]} -gt 0 && "$services_spec" != true ]]; then
  problems+=("proto/*.proto changed but docs/specs/services.md did not.")
fi

if [[ ${#problems[@]} -eq 0 ]]; then
  echo "Spec touched alongside ${#behaviour[@]} behaviour file(s): OK."
  exit 0
fi

# Exemptions: bots (Renovate, Dependabot, release-please via github-actions)
# and the agent's experiment promoter (services/agent/promote: head branch
# agent/experiment-<flag>, title "Flag experiment: <flag>").
author="${PR_AUTHOR:-}" title="${PR_TITLE:-}" head_ref="${PR_HEAD_REF:-}"
if [[ "${PR_AUTHOR_TYPE:-}" == "Bot" ]] \
  || [[ "$author" =~ ^(renovate|dependabot|release-please|github-actions)(\[bot\])?$ ]] \
  || [[ "$title" =~ ^chore\(deps(-dev)?\) ]] \
  || [[ "$title" =~ ^chore\(main\):\ release ]] \
  || [[ "$head_ref" == agent/experiment-* && "$title" == "Flag experiment: "* ]]; then
  echo "Exempt from the spec gate (bot or agent-promoter PR: author=$author, head=$head_ref, title=$title)."
  exit 0
fi

if [[ -n "${PR_LABELS+x}" ]]; then
  labels="$PR_LABELS"
else
  labels="$(gh pr view "$PR_NUMBER" --json labels --jq '[.labels[].name] | join(",")')"
fi
if [[ ",$labels," == *",$LABEL,"* ]]; then
  echo "PR carries the '$LABEL' label: spec gate waived. The PR body must say 'Spec changes: none: <reason>'."
  exit 0
fi

echo "::error title=Spec not updated::This PR changes service behaviour without a spec change. See $HELP"
echo
for p in "${problems[@]}"; do echo "  - $p"; done
echo
echo "Behaviour files in this PR:"
printf '  %s\n' "${behaviour[@]}"
echo
echo "Fix it one of two ways ($HELP):"
echo "  1. Update the spec in this PR (requirements, use case, or docs/specs/*)."
echo "  2. If observable behaviour is unchanged (refactor, test-only, CI-only), add the"
echo "     '$LABEL' label and write 'Spec changes: none: <reason>' in the PR body."
echo "     The label re-runs this check automatically (spec-label-rerun workflow)."
exit 1
