---
name: review-pr
description: >
  Independent reviewer workflow for ONE JoustMania pull request: detached
  review worktree, issue and spec read, correctness and safety first, a
  mutation check (weaken the fix, a test must fail), `make pr-gates`, CI status
  triage (SonarCloud non-blocking, stale-image/collector failures are infra),
  conflict check against main and open PRs, then one ranked verdict. Depth
  scales with PR type (short diff check vs full adversarial review), and a
  delta mode re-checks only <old>..<new> after fix commits. Use when the user
  or orchestrator says "review PR #N", "re-check #N after the fix", or "is #N
  ready to merge". Fixes go to a fresh fixer agent (implement-issue fixer mode).
argument-hint: "#PR [delta <old>..<new>] [previous findings file]"
---

# Review PR (JoustMania)

Review, do not fix. Never push to the PR branch.

## Depth by PR type

| PR type | Depth | Model |
|---|---|---|
| Docs-only, test-only, config/label tweaks | Short check: diff only, gates that apply, no mutation check | `sonnet` |
| Delta re-check of fix commits | Delta mode (below): diff `<old>..<new>` + previous findings | `sonnet` |
| Ordinary feature / bugfix | Full review, mutation check on the main fix | `opus` |
| Hot path (game loop, death detection, motion stream), concurrency, pairing / BlueZ, safety gates (kill-switch, fail-closed flags, interventions), CI / build / release | Full adversarial review: hunt races, fail-open paths, stale-image traps; mutation check on every fix | `opus` |

The orchestrator picks the model from this table; when you are the wrong depth for the PR, say so in one line.

## Hard rules

- Use a detached worktree and remove it at the end. Fetch into a per-PR ref, never `FETCH_HEAD`: parallel reviewers share it, and one review then checks out another PR's head.
  ```bash
  git fetch origin +pull/<PR>/head:refs/review/<PR> && git worktree add --detach .worktrees/review-<PR> refs/review/<PR>
  # ... review ...
  git worktree remove --force .worktrees/review-<PR> && git update-ref -d refs/review/<PR>
  ```
- Never modify the author's worktree, never `git reset --hard` or `git stash` in any shared worktree, never touch
  the running stack. Throwaway containers on free ports with `--rm` are fine.
- Quiet output: `-q --tb=short`, `| tail`. Long notes go to `<scratchpad>/review-<PR>.md`.

## Steps (full review)

1. **Context.** `gh pr view <PR> --json title,body,headRefOid,files,labels`; read the linked issue ("Part of #N")
   and the UC / FR / spec rows the PR names. Check the head SHA matches what you were told.
2. **Correctness and safety first.** Does the diff do what the spec says, nothing more? Fail-closed where it
   must? Async/await propagated? Flags read at use time? Proto RPC has its connect-proxy forwarding method?
   Spec IDs collide with open PRs? `Part of #N`, no closing keywords? Labels (`ci-full`, `no-spec-change`) right?
3. **Mutation check.** In the review worktree, revert or weaken the core fix (`git show <fix> -- <file> | git apply -R`
   or a one-line edit), run the new tests: at least one must FAIL. Restore with `git checkout -- .` (detached
   review worktree only). No failing test = a blocking finding.
4. **Gates.** `make pr-gates` in the review worktree (`BASE=origin/main`).
5. **CI.** `gh pr checks <PR>` once (no watching). SonarCloud is non-blocking. Collector `exited (1)` /
   `unknown type: "span_metrics"`, the fast-path stale-prebuilt-images guard and `blob not found` are
   infrastructure: name them as such (advice: `ci-full` label or rerun), not as PR defects.
6. **Conflicts.** `git merge-tree --write-tree origin/main refs/review/<PR>` for conflicts with main; skim
   `gh pr list --state open --json number,title,files` for PRs touching the same files or spec IDs.

## Delta mode

Given `<old>..<new>` and the previous findings: review only `git diff <old>..<new>`, then state per previous
finding `resolved | partly | not resolved` with file:line. New problems introduced by the delta are findings;
untouched code is out of scope. Run only the gates the delta touches (`make pr-gates SERVICE=...`).

## After the verdict

Findings are fixed by a FRESH, small fixer agent (`implement-issue` fixer mode) that receives only the
findings file, the PR number, head SHA and diff range. Do not hand them back to the original implementer.
Write findings to `<scratchpad>/review-<PR>-findings.md` in that shape.

## Verdict (hand back once, 15 lines or fewer; up to 40 only for blocked PRs)

```
PR #<n> head <sha8>: READY | READY WITH CHANGES | BLOCKED   (depth: short|full|adversarial|delta)
Findings (ranked, most severe first):
  1. [blocker|major|minor] path/file.py:123: <problem> -> <fix>
Mutation check: <what was weakened> -> <test> FAILED as expected | no test failed
Gates: <pr-gates summary>   CI: <green | red: infra (<kind>) | red: <check>>
Conflicts: <none | main | #PR (files)>
Follow-up candidates (out of scope): <short list or none>
Details: <scratchpad>/review-<PR>.md
```
