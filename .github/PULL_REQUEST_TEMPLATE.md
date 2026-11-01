## Summary

Brief description of what this PR does and why.

## Spec changes

<!--
Spec-driven development (AIUP): behaviour changes update the spec first, in this PR or a preceding one.
See docs/CONTRIBUTING.md#spec-driven-development-aiup. Fill in ONE of:
  - the IDs / sections you added or changed, e.g. "FR-045, NFR-011, UC-040 A3, specs/flags.md#agent, specs/telemetry.md agent.decide"
  - "none: <reason>", e.g. "none: refactor, no behaviour change" or "none: test-only"
    and add the `no-spec-change` label, or the Spec Gate CI check fails for PRs touching services/, lib/ or proto/
  Bot PRs (Renovate, release-please, chore(deps)) may delete this section.
-->

Spec changes:

## Changes

- ...

## Test Plan

- [ ] Unit tests pass (`cd services/<name> && uv run pytest`)
- [ ] `make lint` passes
- [ ] Integration tests pass (`make test`) — if applicable
- [ ] New tests trace to the spec IDs above (name the FR/UC in the test docstring or PR body)

## Related Issues

Part of #

## Merge notes

<!--
Read by scripts/ci/merge-sweep.sh after the merge queue merged this PR (#1809). One line each,
outside this comment; {MERGE} becomes the short merge SHA. Leave empty when nothing applies.
Only issues on the "Part of #N" line above are closed or noted (never a PR number).
  close #N: <what was delivered>   closes issue N with this comment, once (a reopened issue stays open);
                                   GitHub may close N at merge itself (closing keyword): the sweep then
                                   only posts the comment, once
  note #N: <text>                  comments on issue N once
Do not also pass --close / --note-issue to safe-merge for the same issue.
-->
