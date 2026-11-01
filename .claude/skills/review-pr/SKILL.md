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

Review, do not fix. Never push to the PR branch. Agents run this as the `pr-reviewer` agent
(`.claude/agents/pr-reviewer.md`), which also carries the standing rules for worktrees, mutation checks and gates.

## Depth by PR type

| PR type | Depth | Model |
|---|---|---|
| Docs-only, test-only, config/label tweaks | Short check: diff only, gates that apply, no mutation check | `sonnet` |
| Delta re-check of fix commits | Delta mode (below): diff `<old>..<new>` + previous findings | `sonnet` |
| Ordinary feature / bugfix | Full review, mutation check on the main fix | `opus` |
| Hot path (game loop, death detection, motion stream), concurrency, pairing / BlueZ, safety gates (kill-switch, fail-closed flags, interventions), CI / build / release | Full adversarial review: hunt races, fail-open paths, stale-image traps; mutation check on every fix | `opus` |

The orchestrator picks the model from this table; when you are the wrong depth for the PR, say so in one line.

**Plan check.** When the brief names a plan file (`<scratchpad>/<N>-plan.md`, see `.claude/agents/implementer.md` "Plan first"),
also check the PR against it (the plan as folded after the opposer, `<scratchpad>/<N>-opposer.md`, when one exists). Report a listed affected path the PR ignores, a listed test that is missing, or an
unexplained deviation as a finding.

## Hard rules

- Use a detached worktree and remove it at the end. Fetch into a per-PR ref, never `FETCH_HEAD`: parallel reviewers share it, and one review then checks out another PR's head.
  ```bash
  git fetch origin +pull/<PR>/head:refs/review/<PR> && git worktree add --detach .worktrees/review-<PR> refs/review/<PR>
  # ... review ...
  git worktree remove --force .worktrees/review-<PR> && git update-ref -d refs/review/<PR>
  ```
- Never modify the author's worktree, never `git reset --hard` or `git stash` in any shared worktree, never touch
  the running stack. Throwaway containers on free ports with `--rm` are fine.
- Known local flakes (a failure that matches is named, not blamed on the PR): [`.claude/known-flakes.md`](../../known-flakes.md).
- Curated lessons (mutation recipes, gate-run and flag traps, CI and queue behaviour, coordinator and agent patterns, known false positives): read [`.claude/review-lessons.md`](../../review-lessons.md) before a full or adversarial review, and before a short review of a docs PR.
- Quiet output: `-q --tb=short`, `| tail`. Long notes go to `<scratchpad>/review-<PR>.md`.

## Steps (full review)

1. **Context.** `gh pr view <PR> --json title,body,headRefOid,files,labels`; read the linked issue ("Part of #N")
   and the UC / FR / spec rows the PR names. Check the head SHA matches what you were told.
2. **Correctness and safety first.** Does the diff do what the spec says, nothing more? Fail-closed where it
   must? Async/await propagated? Flags read at use time?
   **C-035:** a new env-only tunable is a finding; runtime flexibility is a flag (requirements.md C-035 lists the exceptions). Proto RPC has its connect-proxy forwarding method?
   Spec IDs collide with open PRs? `Part of #N`, no closing keywords? Labels (`ci-full`, `no-spec-change`) right?
   **Merge-gate edits (#1809, accepted risk):** the merge queue runs `.github/workflows/*.yml` from the queue
   commit, so a PR that edits `.github/workflows/**` or `scripts/ci/review-gate.sh` can make the `Reviewed head`
   gate pass itself. Review every such hunk adversarially (whatever the PR type): the gate must stay
   `merge_group`-only, read-only, fail closed and keyed to the PR's current head; name the files in the verdict.
3. **Mutation check.** In a SEPARATE detached worktree (`review-<PR>-mut`; the `pr-reviewer` agent rules), revert or weaken the core fix (`git show <fix> -- <file> | git apply -R`
   or a one-line edit), run the new tests: at least one must FAIL (hand the run to the Haiku `gate-runner` agent with
   `run x1 expect fail -- <command>`; it reports `MUTATION CAUGHT (assertion)`, `CAUGHT? (non-test error)` (not
   proof: read the log) or `MUTATION NOT CAUGHT`). Remove that worktree afterwards (never `git checkout <ref> -- <file>` in a worktree that holds work). No failing test = a blocking finding.
4. **Gates.** `make pr-gates` in the review worktree (`BASE=origin/main`), run through `gate-runner`
   (`<review worktree>` + `gates BASE=origin/main`) so only its summary enters your context. Call gate-runner
   in the foreground and never start gates with `run_in_background` (#2036: the agent idles and hands back early).
   **Exception, diff only under `scripts/**`:** no `make pr-gates` (it selects the container service gate). Run
   `make lint` and the relevant `scripts/tests/test-*.sh` yourself with the fail-loud stub first on `PATH`:
   `unset COMPOSE DOCKER; PATH=<repo>/scripts/tests/lib/fail-docker:$PATH FLAG_SERVICE_URL=http://127.0.0.1:9 bash scripts/tests/test-X.sh`.
   A mutation of a service-tier test runs `make test-service SERVICE_TESTS=<file>`.
5. **CI.** `gh pr checks <PR>` once (no watching). Local `make pr-gates` no longer runs the service tier for product
   paths: when the diff touches the CI `service_tests` paths (lib, proto, coordinator, menu, controller_manager,
   flagd, flag-service, `tests/service`, ...), "Service Tests" must be `pass`, not skipped or pending, before
   "ready". A Service Tests failure that passes on rerun is a finding (flake or race), not infrastructure. SonarCloud is non-blocking. Collector `exited (1)` /
   `unknown type: "span_metrics"`, the fast-path stale-prebuilt-images guard and `blob not found` are
   infrastructure: name them as such (advice: `ci-full` label or rerun), not as PR defects.
6. **Conflicts.** `git merge-tree --write-tree origin/main refs/review/<PR>` for conflicts with main; skim
   `gh pr list --state open --json number,title,files` for PRs touching the same files or spec IDs.
7. **Spec review** (every depth, when the PR touches `docs/use_cases/`, `docs/requirements.md`,
   `docs/entity_model.md` or `docs/glossary.md`). Run the AIUP spec review on each changed use case, following
   `scripts/aiup/spec-review/SKILL.md`:
   - Part A: `python3 -I scripts/aiup/spec-review/scripts/spec_lint.py --docs docs --only UC-xxx`.
   - Part B: the semantic checklist in `scripts/aiup/spec-review/references/review-checklist.md`, on the changed text.

   Report only warnings the PR **introduces or leaves contradicting its own change** as findings. Pre-existing
   warnings go under "Follow-up candidates". Part B is advisory: never block on it alone, except a new
   contradiction between use cases, which is a `major` finding.

## Delta mode

Given `<old>..<new>` and the previous findings: review only `git diff <old>..<new>`, then state per previous
finding `resolved | partly | not resolved` with file:line. New problems introduced by the delta are findings;
untouched code is out of scope. Run only the gates the delta touches (`make pr-gates SERVICE=...`).
A READY delta verdict names the NEW full head: the review status of `<old>` never carries over to `<new>`.

## After the verdict

**Recording the reviewed head (#1809).** A READY verdict names the full 40-hex head you reviewed (re-read
`gh pr view <PR> --json headRefOid` right before the verdict; a moved head means re-review, not READY). That line is
the record: the orchestrator hands it to the `pr-merger` agent as `arm <PR> <full sha>`, which posts the
`Reviewed head` status on exactly that SHA and arms auto-merge; GitHub merges from the queue. You never arm, post
the status or merge yourself. A later push leaves the new head without a status, so GitHub cannot merge it until a
delta review is READY and the new head is armed.

Findings are fixed by a FRESH, small fixer agent (`implement-issue` fixer mode) that receives only the
findings file, the PR number, head SHA and diff range. Do not hand them back to the original implementer.
Write findings to `<scratchpad>/review-<PR>-findings.md` in that shape.

## Verdict (hand back once, 15 lines or fewer; up to 40 only for blocked PRs)

```
PR #<n> head <full 40-hex sha>: READY | READY WITH CHANGES | BLOCKED   (depth: short|full|adversarial|delta)
Findings (ranked, most severe first):
  1. [blocker|major|minor] path/file.py:123: <problem> -> <fix>
Mutation check: <what was weakened> -> <test> FAILED as expected | no test failed
Gates: <pr-gates summary>   CI: <green | red: infra (<kind>) | red: <check>>   Service Tests: <pass | skipped | n/a>
Conflicts: <none | main | #PR (files)>
Cleanup: <review worktrees and refs/review/<PR> removed>
Follow-up candidates (out of scope): <short list or none>
Details: <scratchpad>/review-<PR>.md
```
