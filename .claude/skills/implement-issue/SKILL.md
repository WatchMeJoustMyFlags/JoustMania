---
name: implement-issue
description: >
  End-to-end implementer workflow for ONE JoustMania GitHub issue or PR task:
  assign the issue, isolated worktree from origin/main, spec-first (AIUP) edits,
  a test that fails without the fix, `make pr-gates`, Conventional Commit, push,
  PR with "Part of #N", labels added afterwards, then STOP (no CI watching, no
  merge) and hand back one short report. Also has a "fixer mode" for applying a
  reviewer's findings to an existing PR. Use when an orchestrator or the user
  says "implement #1234", "fix issue #N", "open a PR for ...", or "apply these
  review findings to PR #N". For one use case end to end, /implement and the
  test skills hold the per-layer detail.
argument-hint: "#ISSUE [scope / decisions] | fixer PR#N <head sha> <old>..<new> <findings file>"
---

# Implement Issue (JoustMania)

The brief gives you the issue, scope and decisions. Everything else is here: do not re-ask for it.

## Hard rules

- Worktree only: `.worktrees/<slug>` from `origin/main`. Never edit the main checkout, other worktrees
  (other sessions own them) or the running docker stack. Throwaway containers with `--rm` on free ports are fine.
- Never `git stash` in any form: the stash stack is shared by every worktree of the clone.
- Scratch files go in the session scratchpad, named with the issue number (`<scratchpad>/1234-body.md`).
- Always pass the explicit PR number: `gh pr edit <n>`, `gh pr view <n>`; never rely on the current branch.
- Push, then STOP. Do not watch CI, do not `gh run watch`, do not merge, do not "wait on background work".
  After a READY review the orchestrator has the `pr-merger` agent arm the reviewed head (`arm <PR> <full sha>`,
  #1809) and GitHub merges from the queue. Hand back ONCE, never re-send.

## Steps

1. **Claim.** `gh issue edit <N> --add-assignee aepfli`. Read the issue and its comments.
2. **Worktree.**
   ```bash
   git fetch origin && git worktree add .worktrees/<slug> -b <type>/<slug> origin/main
   ```
   Work with absolute paths inside it; confirm with `git -C <wt> branch --show-current`.
3. **Spec first (AIUP, CLAUDE.md).** If the brief names a plan file (`<scratchpad>/<N>-plan.md`), read it first
   (`.claude/agents/implementer.md`, "Plan first"). When observable behaviour changes:
   - update the UC / FR / NFR text and `docs/specs/*` (flags, telemetry, services, decide contract) in the same PR;
   - new IDs take the next free number: `grep` docs AND `gh pr list --state open --search "UC-0xx"` / the open
     PR diffs for collisions; when agents run in parallel on one UC, reserve A-/BR- ranges in the brief;
   - never renumber or delete: mark `Obsolete` / `Rejected`;
   - no behaviour change: `Spec changes: none: <reason>` + the `no-spec-change` label.
4. **Test first.** At least one new test must FAIL without the fix: write it, run it red, then fix.
   Tag it: `@pytest.mark.uc("UC-040", flow="A2", br=[...])`, Go `// UC: UC-040 A2` + `UC-040/A2 ...`
   subtests, vitest `describe('UC-040: ...')`, shell `# UC:` header (`.claude/skills/README.md`).
5. **Implement** the smallest change that satisfies the spec. Per-layer detail: `/implement`, `/pytest-test`,
   `/go-test`, `/integration-test`.
6. **Gates.** `make pr-gates` (auto-detects from the diff; `SERVICE=menu,agent` to force). Fix every FAIL.
   Agents send gate and test-suite runs to the Haiku `gate-runner` agent (`.claude/agents/gate-runner.md`),
   which keeps the logs out of your context; the `implementer` agent says when.
   The service tier (T2) runs locally only for harness / stack changes (full) or for the `tests/service/**/test_*.py`
   files you edit (`make test-service SERVICE_TESTS=<files>`). For product paths (lib, coordinator, menu, ...) the
   full-tier evidence is PR CI "Service Tests" = pass; never report a local `make pr-gates` PASS as tier coverage.
   Integration (`make test-dev`) only when the brief asks or the change is cross-service.
7. **Commit.** Conventional Commits (`fix(menu): ...`, `feat(agent): ...`), small and focused, ending with
   the attribution lines from the session's system reminder (never a hard-coded model name). A failing pre-commit hook may
   have reformatted files: re-stage and make a NEW commit.
8. **PR.**
   ```bash
   git push -u origin HEAD
   gh pr create --base main --title "<conventional title> (#N)" --body-file <scratchpad>/<N>-body.md
   gh pr edit <PR> --add-label <labels>     # labels start no CI run (#1527); `ci-full` re-runs it in full
   ```
   Body follows `.github/PULL_REQUEST_TEMPLATE.md`: Summary, `Spec changes:`, Changes, Test Plan,
   `Part of #N` (never `closes`/`fixes`), last line `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
   Labels: `ci-full` when Dockerfiles, images, protos or dependencies change; `no-spec-change` when no spec applies.
9. **Report** (below) and end.

## Gotchas

| Area | Rule |
|---|---|
| Python tests | `cd services/<svc> && uv run --extra test pytest -q --tb=short 2>&1 \| tail -20` (verified for lib and all six services, incl. pairing_daemon and controller_manager; if a workspace import fails, fall back to `uv run --all-packages pytest`). `make pr-gates` encodes it |
| Quiet output | Always `-q --tb=short`, piped through `tail`; never dump full logs into context |
| Go | `go vet ./... && go test -race ./...` in `services/agent` / `services/flag-service` |
| connect-proxy | Go tests only run in the Docker builder stage (`docker build --target builder -f services/connect-proxy/Dockerfile .`) |
| Protos | A `.proto` field change needs `make protos`. A new RPC also needs a forwarding method in `services/connect-proxy/main.go`, and dashboard `src/gen/*_pb.ts` is hand-written: update it by hand |
| Dashboard | Node 24 (`~/.nvm/versions/node/v24.*`): `npx tsc --noEmit && npx vitest run` |
| Spec lint | `TBD:` is a weak word: write `TBD-A<n>`. REQ_STATUS_DRIFT: an FR can't sit below its UC's status. An Obsolete FR stays linked from its UC (only rejected/deferred count as inactive) |
| Flags | Read flags at use time, not init; safety gates fail CLOSED; flag change = `docs/specs/flags.md` row |
| Rollout | `.env` pins a release `IMAGE_TAG`: the Pi keeps running the release until the next one ships. Say so in the PR when behaviour on the Pi matters |
| Generated specs | `docs/specs/generated/*.md` come from the code (`make specs`); never hand-edit them, put notes in `docs/specs/notes/*.yaml`. After a rebase that conflicts in a generated spec file, take either side and run `make specs`. A new metric, RPC or flag-file change: run `make specs` before `make pr-gates` (`make specs-check` gates CI) |
| YAML | Compose / collector edits: `make pr-gates` runs `docker compose config -q` and a duplicate-key check |

## Fixer mode (review findings on an existing PR)

The orchestrator starts a FRESH, small agent with only: PR number, head SHA, the reviewer's findings file and the
diff range. Do not resume the original implementer and do not re-read the whole issue history.

1. Reuse the PR's worktree only if this session owns it; otherwise
   `git worktree add .worktrees/<slug>-fix origin/<branch>` (a branch checkout, not detached).
2. Confirm `git rev-parse HEAD` matches the given head; if not, stop and report the mismatch.
   **Queued or armed PR (#1809):** GitHub rejects a push to a queued branch (GH006), and an armed head would merge
   without your fix. Before the first push, check `gh pr view <PR> --json autoMergeRequest,isInMergeQueue` and, when
   either is set, withdraw it: `scripts/ci/safe-merge.sh <PR> <full head> --revoke` (the newest status wins, so
   no sweep re-arms the old head), then dequeue (`gh api graphql -f query='mutation($id:ID!){dequeuePullRequest(input:{id:$id}){clientMutationId}}' -f id=$(gh pr view <PR> --json id --jq .id)`)
   when queued, or `gh pr merge <PR> --disable-auto` when only armed. A bare dequeue is not enough: the merge sweep
   would re-arm the still-reviewed old head. Say so in the report. The pushed head has no status, so it needs a
   delta review and a new arm.
3. Fix exactly the listed findings, each with a test where it is a behaviour bug. Nothing else.
4. `make pr-gates` (via `gate-runner`), commit (`fix: address review on #<PR>` + the session's attribution lines), push. No force-push unless told.
5. Report: one line per finding: `fixed <sha> | not fixed: <reason>`, plus `head moved to <sha>: needs delta review`
   (and `revoked + dequeued` / `revoked + disarmed` when step 2 applied).

## Report (hand back once, 15 lines or fewer)

Write the long version (full gate output, notes, logs) to `<scratchpad>/<N>-report.md` and reference it.

```
PR #<n> <url>  head <sha8>  branch <branch>
Changed: <3-5 bullets, file areas + what>
Spec: <IDs touched | none: reason>
Fails without fix: <test id(s)>
Gates: <pr-gates summary: PASS n / FAIL n / SKIP n; failing gate names>
Labels: <labels>
Deferred: <follow-ups, issue numbers if filed>
Decisions needed: <none | question>
Details: <scratchpad>/<N>-report.md
```
