# Claude Code Guidelines for JoustMania

Multiplayer motion-controlled party game system using PS Move controllers.

**Main branch: `main`**

## Spec-driven development (AIUP)

Specs are the source of truth; code and tests follow from them. Start at [docs/README.md](docs/README.md).

- **Before any feature or bugfix that changes behaviour**, update the spec FIRST, in the same PR (or a preceding one): [docs/requirements.md](docs/requirements.md) (FR/NFR/C), the affected [docs/use_cases/UC-xxx.md](docs/use_cases/) (+ [docs/use_cases.puml](docs/use_cases.puml) for a new UC), and the matching technical spec in [docs/specs/](docs/specs/) (`services.md`, `flags.md`, `telemetry.md`, `decide-contract.md`). New flag, span, metric, RPC or decide field = spec row first.
- **Use the AIUP skills** (`aiup-core` plugin, enabled at project scope in `.claude/settings.json` since #1259; per-user opt-out in `.claude/settings.local.json`) and follow their formats: `requirements` (catalog), `entity-model` (ER + 5-column attribute tables), `use-case-diagram` (PlantUML), `use-case-spec` (one UC per file). Never renumber IDs; retire with status `Rejected` / `Obsolete`.
- **Spec describes what the code does.** Planned work carries its issue number and status `Open`/`In Progress`. If code and spec disagree, fix one of them or log it under "Discrepancies found" in [docs/specs-inventory.md](docs/specs-inventory.md).
- **Logged discrepancies (`D-*` rows):** for rows of kind `bug` / `likely bug`, the CODE is wrong. The fixing PR marks the row `Resolved by #N`, updates the affected FR/NFR status, and rewrites any use-case or spec text that describes the buggy behaviour. For rows of kind `stale doc`, the doc is wrong: fix or banner the doc.
- **No spec change needed** when observable behaviour is unchanged: pure refactors (same RPCs, flags, spans, metrics, outcomes), test-only changes, dependency bumps, CI-only changes, typo fixes. Say `Spec changes: none: <reason>` in the PR body.
- **Hotfixes** may ship code first; the spec follow-up PR must land within 3 days and the gap is logged in [docs/specs-inventory.md](docs/specs-inventory.md) until then.
- **Bot PRs** (Renovate, release-please, `chore(deps)`) are exempt from the "Spec changes" section.
- **Construction loop** (project skills in [.claude/skills/](.claude/skills/README.md)): work one use case at a time with `/spec-review UC-xxx` (`spec-review` and `test-case` come from the `aiup-core` plugin, and `spec-review` is also vendored in `scripts/aiup/spec-review/`; CI runs the same lint as the Spec Gate, see CONTRIBUTING; run the semantic review again before every Tested flip and in every PR review touching specs) → `/implement UC-xxx` → `/pytest-test` | `/go-test UC-xxx` → `/integration-test UC-xxx` → `/coverage-check UC-xxx`. New tests carry the traceability tag (`@pytest.mark.uc("UC-040", flow="A2", br=[...])`, Go `// UC: UC-040 A2` + `UC-040/A2 …` subtests; pre/postcondition tests add `cond=["Pre-1"]`); a UC is `Tested` only when every main scenario and alternative flow has a tagged test.
- Every other PR fills the **Spec changes** section of the PR template. The "tests trace to spec IDs" checklist item applies to new tests only. Workflow details: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md#spec-driven-development-aiup).

## Agent workflows

- Implementing an issue (agent or human): [.claude/skills/implement-issue](.claude/skills/implement-issue/SKILL.md): worktree, spec first, failing test, `make pr-gates`, push, stop, 15-line report. Agents run it as the [implementer](.claude/agents/implementer.md) agent (sonnet; opus for hot path, concurrency, safety), which hands gate and test runs to the Haiku [gate-runner](.claude/agents/gate-runner.md) and broad searches to `Explore`.
- Reviewing a PR: the [pr-reviewer](.claude/agents/pr-reviewer.md) agent runs [.claude/skills/review-pr](.claude/skills/review-pr/SKILL.md): depth by PR type, mutation check, delta mode; a READY verdict names the full reviewed head. GitHub owns merging after review (#1809): the Haiku [pr-merger](.claude/agents/pr-merger.md) agent arms that head (`arm <PR> <sha>` = `scripts/ci/safe-merge.sh --arm`, which posts the `Reviewed head` status and enables auto-merge) and stops; GitHub queues and merges. A later `sweep` (`scripts/ci/merge-sweep.sh`) dequeues UNMERGEABLE entries, re-arms an ejected reviewed head at most once, closes the issues in the PR's `## Merge notes` and removes merged worktrees. A fixer pushing to an armed or queued PR revokes and dequeues first (implement-issue fixer mode). The old wait-and-merge path stays until #1809 PR3. Reviewers send `make pr-gates` and mutation-check runs to gate-runner too.

## Session Management

When resuming from a previous session, ask the user to confirm the current task before reading files or starting work. Do not assume context from prior sessions without verification.

## Git Workflow

Always create a feature branch before making changes. Never commit directly to main. Verify current branch with `git branch --show-current` before starting work.

## Issue Tracking

When work on a GitHub issue starts (implementation begun or a background agent launched), assign the issue to the repository owner so in-flight work is visible at a glance:

```bash
gh issue edit <NUMBER> --add-assignee aepfli
```

Closing an issue after its PR merges:

- **Armed flow (#1809):** nobody closes or comments by hand. Put `close #N: <what was delivered>` / `note #N: <text>` lines in the PR body's `## Merge notes` section (`{MERGE}` becomes the short merge SHA). `close #N` is a GitHub closing keyword, so GitHub closes the issue at merge; the merge sweep then posts the delivery comment once, only when the issue's last close came from this PR or its merge commit (#2508) and no trusted comment after the merge already names the PR (#2509). An issue closed by anything else is left alone. `note #N` lines become comments.
- **Old wait-and-merge path, or a PR without merge notes:** close the issue by hand with a comment that names the PR and what was delivered (`gh issue close <N> --comment "..."`).

Keep the `Part of #N` line in the PR body: the sweep acts only on the issues listed there.

## Quick Reference

```bash
make lint          # Lint code
make test          # Run integration tests (docker compose)
make protos        # Regenerate proto files after .proto changes
```

## Testing

**Integration tests** run with docker compose:
```bash
make test                    # Run all integration tests
SKIP_TEARDOWN=1 make test    # Keep docker running after tests (for debugging)
```

**Unit tests** run with uv from each service directory:
```bash
cd services/<service-name>
uv run pytest
```

## Git Worktree Workflow

**Always create a new worktree for changes.** Never commit directly to the main checkout directory.

```bash
git worktree add .worktrees/issue-<NUMBER> -b fix/description origin/main
cd .worktrees/issue-<NUMBER>
```

Worktrees live under `.worktrees/` in the main checkout, never in `/tmp/` or a sibling directory.

This keeps the main checkout clean and isolates changes when multiple agents work in parallel.

## Rust

Always run Clippy checks (`cargo clippy -- -D warnings`) before committing Rust code. Watch for unnecessary borrows, redundant closures, and other common Clippy lints.

## CI / Pre-push Checklist

When working on CI/build fixes, run the full CI check locally before pushing (linting, type checks, tests). For Go: verify module versions and imports. For Rust: run with `--test-threads=1` if needed. For JS/TS: ensure @types/node is included.

## Configuration Files

When editing YAML config files (Docker Compose, OTEL Collector, dashboards), validate for duplicate keys and correct field names before committing. Use `python -c "import yaml; yaml.safe_load(open('file.yaml'))"` as a quick check.

## Key Documentation

- [Northstar](docs/NORTHSTAR.md) - The "dark factory" goal and its hard constraints; read before agent work
- [Docs index](docs/README.md) - Start here: vision → requirements → use cases → specs → runbooks → research
- [Contributing Guide](docs/CONTRIBUTING.md) - Development workflow, CI checks, code style
- [Development Guide](docs/DEVELOPMENT.md) - Building, running, debugging services
- [Services spec](docs/specs/services.md) - Services, ports, RPCs, dependencies
- [Ownership Model](docs/OWNERSHIP_MODEL.md) - Human/agent settings arbitration & composition rules
- [Talk runbook](docs/talk/two-standards-runbook.md) - "Two Standards" stage demo: beats, flag flips, pre-flight (`scripts/talk-preflight.sh`)
