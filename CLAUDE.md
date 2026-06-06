# Claude Code Guidelines for JoustMania

Multiplayer motion-controlled party game system using PS Move controllers.

**Main branch: `main`**

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

- [Contributing Guide](docs/CONTRIBUTING.md) - Development workflow, CI checks, code style
- [Development Guide](docs/DEVELOPMENT.md) - Building, running, debugging services
- [Architecture](docs/ARCHITECTURE.md) - System design and service interactions
