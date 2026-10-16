# Claude Code Guidelines for JoustMania

Multiplayer motion-controlled party game system using PS Move controllers.

**Main branch: `main`**

## Spec-driven development (AIUP)

Specs are the source of truth; code and tests follow from them. Start at [docs/README.md](docs/README.md).

- **Before any feature or bugfix that changes behaviour**, update the spec FIRST, in the same PR (or a preceding one): [docs/requirements.md](docs/requirements.md) (FR/NFR/C), the affected [docs/use_cases/UC-xxx.md](docs/use_cases/) (+ [docs/use_cases.puml](docs/use_cases.puml) for a new UC), and the matching technical spec in [docs/specs/](docs/specs/) (`services.md`, `flags.md`, `telemetry.md`, `decide-contract.md`). New flag, span, metric, RPC or decide field = spec row first.
- **Use the AIUP skills** (`aiup-core` plugin) and follow their formats: `requirements` (catalog), `entity-model` (ER + 5-column attribute tables), `use-case-diagram` (PlantUML), `use-case-spec` (one UC per file). Never renumber IDs; retire with status `Rejected` / `Obsolete`.
- **Spec describes what the code does.** Planned work carries its issue number and status `Open`/`In Progress`. If code and spec disagree, fix one of them or log it under "Discrepancies found" in [docs/specs-inventory.md](docs/specs-inventory.md).
- **Pure refactors** (no observable behaviour change: same RPCs, flags, spans, metrics, outcomes) need no spec change; say `Spec changes: none: refactor, no behaviour change` in the PR body.
- Every PR fills the **Spec changes** section of the PR template. Workflow details: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md#spec-driven-development-aiup).

## Session Management

When resuming from a previous session, ask the user to confirm the current task before reading files or starting work. Do not assume context from prior sessions without verification.

## Git Workflow

Always create a feature branch before making changes. Never commit directly to main. Verify current branch with `git branch --show-current` before starting work.

## Issue Tracking

When work on a GitHub issue starts (implementation begun or a background agent launched), assign the issue to the repository owner so in-flight work is visible at a glance:

```bash
gh issue edit <NUMBER> --add-assignee aepfli
```

When the implementing PR merges, close the issue manually with a comment referencing the PR and what was delivered — PR bodies in this repo use "Part of #N" phrasing without `closes` keywords, so issues never auto-close.

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
git worktree add ../JoustMania-issue-<NUMBER> -b fix/description origin/main
cd ../JoustMania-issue-<NUMBER>
```

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
- [Architecture](docs/ARCHITECTURE.md) - System design and service interactions
- [Ownership Model](docs/OWNERSHIP_MODEL.md) - Human/agent settings arbitration & composition rules
- [Talk runbook](docs/talk/two-standards-runbook.md) - "Two Standards" stage demo: beats, flag flips, pre-flight (`scripts/talk-preflight.sh`)
