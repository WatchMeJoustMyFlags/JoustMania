# Contributing to JoustMania

Thank you for your interest in contributing to JoustMania! This guide will help you set up your development environment and understand our CI/CD workflow.

## Development Setup

### Prerequisites

- **Docker** - All development tooling runs in containers
- **Git** - Version control
- **VS Code** (optional) - Recommended for dev container support

### Quick Start

#### Option 1: VS Code Dev Container (Recommended)

1. Install [VS Code](https://code.visualstudio.com/) and the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)
2. Clone the repository
3. Open in VS Code
4. When prompted, click "Reopen in Container"
5. Wait for the dev container to build (~5 minutes first time)
6. You're ready to develop!

The dev container includes:
- Python 3.14 (`python:3.14-slim`; the repo requires `>=3.14,<3.15`)
- All development tools (ruff, uv, ipython)
- Docker-in-Docker for building service images
- Pre-configured VS Code extensions

#### Option 2: Local Development

1. Clone the repository:
   ```bash
   git clone https://github.com/WatchMeJoustMyFlags/JoustMania.git
   cd JoustMania
   ```

2. Install uv (Python package manager):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. Run linting/formatting checks:
   ```bash
   make lint      # Check code
   make format    # Auto-format code
   ```

## Spec-driven development (AIUP)

JoustMania follows the [AI Unified Process (AIUP)](https://aiup.dev):
**the specification is the source of truth, and code and tests follow from it.**
Every change that alters observable behaviour updates the spec *before* the code,
in the same PR or in a preceding one. Start at the [docs index](README.md).

### The spec tree

| Layer | File(s) | AIUP skill | Holds |
|---|---|---|---|
| Vision | [vision.md](vision.md) (+ [NORTHSTAR.md](NORTHSTAR.md)) | (none) | Why the product exists, stakeholders, northstar constraints |
| Requirements | [requirements.md](requirements.md) | `requirements` | `FR-xxx` user stories, `NFR-xxx` measurable qualities, `C-xxx` constraints |
| Entity model | [entity_model.md](entity_model.md) | `entity-model` | Domain entities, ER diagram, 5-column attribute tables |
| Use cases | [use_cases.puml](use_cases.puml), [use_cases/UC-xxx.md](use_cases/) | `use-case-diagram`, `use-case-spec` | Actor goals, main success scenario, alternative flows, business rules |
| Technical specs | [specs/services.md](specs/services.md), [specs/flags.md](specs/flags.md), [specs/telemetry.md](specs/telemetry.md), [specs/decide-contract.md](specs/decide-contract.md) | (repo convention) | Ports, RPCs, flag catalog, span / metric / attribute names, the decision contract |

The AIUP skills come from the `aiup-core` plugin, enabled for everyone at project
scope in `.claude/settings.json` (#1259, marketplace `martinellich/aiup-marketplace`,
auto-update off). It provides four skills: `requirements`, `entity-model`,
`use-case-diagram` and `use-case-spec`. The plugin also brings the Context7 MCP
server (`plugin:aiup-core:context7`), which stays enabled by maintainer decision;
disable it per user if you do not want it. To opt out of the whole plugin for
yourself, set `"enabledPlugins": {"aiup-core@aiup-marketplace": false}` in your
git-ignored `.claude/settings.local.json`. Use the skills when you write or extend the corresponding
artefact so the format stays uniform; the technical specs are a JoustMania
convention on top of AIUP, because flags, telemetry names and the decide contract
are where docs drifted from code in the past.

### Workflow for a behaviour change

1. **Find the spec.** Look up the requirement(s) and use case(s) your change
   touches (grep the issue's keywords in `docs/requirements.md` and
   `docs/use_cases/`). If none exist, the change is new behaviour: add them.
2. **Update the spec first.**
   - New or changed capability: add/edit an `FR-xxx` row (user story format, status
     `Open` or `In Progress`).
   - New or changed quality target (latency, rate, budget): add/edit an `NFR-xxx`
     row with a number and, for measured values, the source (issue / PR / test).
   - New or changed flow: edit the use case (main scenario or a new alternative
     flow `An`), reference the FR IDs it fulfils.
   - New or changed flag, span, attribute, metric, RPC, decide question or
     outcome: edit the row in `docs/specs/*.md` *first*; the code must match it.
3. **Implement and test.** New tests carry a traceability tag for the spec unit they
   cover: `@pytest.mark.uc("UC-040", flow="A2", br=["BR-001"])` in pytest, a
   `// UC: UC-040 A2` line above `func Test…` plus `UC-040/A2 …` subtest names in Go,
   `describe('UC-040: …')` in the dashboard (see [.claude/skills/README.md](../.claude/skills/README.md)).
   A docstring or comment alone does not count. Existing tests are retagged per area
   (backlog); a UC returns to `Tested` when `/coverage-check` shows full tagged coverage.
4. **Close the loop.** When the code ships, move the status forward
   (`Open` → `Implemented` → `Verified`, where an FR is `Verified` only when every use case that lists it
   under `Requirements:` is `Tested`; see [Status vocabularies](#status-vocabularies)) and
   update the "Traceability" line of the technical spec section with the code
   path and test. In a same-PR spec change, set the post-merge status
   (`Implemented` / `Verified`) directly; do not leave `In Progress` for a
   follow-up to flip.
5. **Fill in the PR template's "Spec changes"** with the IDs/sections changed.

The spec PR can precede the code PR (useful for review of intent), but a code PR
that changes behaviour without a spec change is incomplete.

**Fixing a logged discrepancy.** A `D-*` row in
[specs-inventory.md](specs-inventory.md#4-discrepancies-found) is either a `stale doc` (the doc is
wrong: fix or banner it) or a `bug` / `likely bug` (the code is wrong). A PR that fixes a bug row marks
it `Resolved by #N`, updates the status of the affected requirement, and rewrites any use-case or spec
text that still describes the buggy behaviour (e.g. UC-040 A11 for D-A9).

**Hotfixes** may merge code first; the spec follow-up PR lands within 3 days and the gap is logged in
the inventory until then.

### Status vocabularies

Requirements use the AIUP requirement statuses (`Open`, `In Progress`, `Implemented`, `Verified`,
`Deferred`, `Rejected`); use cases use the use-case statuses, which map as follows: `Draft` / `Review` /
`Approved` = requirement `Open` or `In Progress`, `Implemented` = `Implemented`, `Tested` / `Done` =
`Verified`, `Obsolete` = `Rejected`. UC `Tested` = every main-scenario and alternative-flow step has a
tagged test (see the `/coverage-check` skill, added by #1289). A use case's status is never above the weakest FR on its
`Requirements:` line; FRs that only a planned flow needs go on a separate `Planned:` line with their
issue number, so the use case can be `Implemented` while they stay `Open`. Conversely, an FR's status is
derived from its use cases: it is `Verified` only when every use case that lists it under `Requirements:` is
`Tested`, otherwise at most `Implemented`. NFRs and constraints are exempt from both rules: NFRs are measured separately, constraints are verified by
review and the technical specs; only FRs derive their status from their use cases.

### Identifiers in use cases (documented deviation)

The `use-case-spec` skill asks for flows without implementation details. JoustMania's use cases
deliberately name flags, span names and event types in their steps and business rules, because those
identifiers are the contract the agent, the dashboard and the tests depend on, and naming them keeps
traceability cheap. Every use case also ends with a "Code / tests" line.

### Pure refactors and non-behavioural changes

No spec change is needed when observable behaviour is unchanged: the same RPCs,
messages, flags (keys, variants, defaults), spans, attributes, metrics, log lines
that tests or dashboards rely on, and the same decision outcomes. Typical cases:
internal refactors, test-only changes, dependency bumps, CI-only changes, typo fixes.
Bot PRs (Renovate, release-please, `chore(deps)`) are exempt from the "Spec changes" section.
Write `Spec changes: none: <reason>` in the PR body
(e.g. `none: refactor, no behaviour change`). If you are unsure whether
something is observable, it probably is: update the spec.

### Traceability IDs

- **IDs are permanent.** Never renumber or reuse an ID. Retire a requirement with
  status `Rejected` (or `Deferred`), a use case with status `Obsolete`, and point
  to the successor.
- **Allocate the next free ID in the right block.** `requirements.md` groups FRs
  by area in blocks of ten-ish IDs (pairing `FR-001`..., lobby `FR-010`...,
  and so on; see its header); append new IDs at the end of the area's block or,
  if the block is full, at the end of the table with the next free number.
- **Use cases list the FRs they realise** (`Requirements:` line in the
  overview; FRs for planned flows on a `Planned:` line). Every FR must be
  referenced by at least one UC.
- **Technical spec sections end with a `Traceability:` line** listing FR / UC IDs,
  the code paths (`file:line` or file) and the tests that exercise them.
- **Planned work** carries its issue number in the row (`#1229`) and stays
  `Open` until merged. Never write a plan as shipped.
- **Discrepancies** between docs and code go to
  [specs-inventory.md](specs-inventory.md#4-discrepancies-found) until fixed.

## Development Workflow

### Git Worktree (Required)

Always work in a new worktree on a feature branch; never commit on `main` or in the
main checkout. Branches are named `fix/<description>` or `feat/<description>` (other
Conventional Commit types such as `docs/` and `refactor/` work the same way):

```bash
# Create worktree for an issue
git worktree add ../JoustMania-issue-<NUMBER> -b fix/<description> origin/main

# Work in the isolated directory
cd ../JoustMania-issue-<NUMBER>

# After PR is merged, clean up
git worktree remove ../JoustMania-issue-<NUMBER>
```

### Making Changes

1. Create the worktree and branch above (`fix/<description>` or `feat/<description>`).

2. Make your changes

3. Format code:
   ```bash
   make format
   ```

4. Run quick checks before committing:
   ```bash
   make ci-quick
   ```

5. Commit changes:
   ```bash
   git add .
   git commit -m "feat: Add my feature"
   ```

6. Push and create pull request:
   ```bash
   git push -u origin fix/<description>
   ```

## Continuous Integration

All pull requests must pass CI checks before merging:

1. **Python Linting** - Code must pass `ruff check` and `ruff format --check`
2. **Type Checking** - Ty type checking (currently warnings only)
3. **Dockerfile Linting** - All Dockerfiles must pass hadolint validation
4. **Proto Validation** - Proto files must be up-to-date (run `make protos`)
5. **Docker Builds** - All services must build successfully
6. **Package Validation** - All workspace packages must install correctly

### Running Checks Locally

**Using Make (recommended):**

```bash
# Lint and format check
make lint
make format-check

# Run both (pre-commit)
make check

# Auto-fix formatting
make format

# Regenerate protos after changes
make protos

# Run integration tests
make test
```

**Using Make targets:**

```bash
# Lint Python code
make lint

# Check formatting
make format-check

# Lint Dockerfiles
make ci-lint-dockerfiles

# Validate protos
make ci-validate-protos
```

**In Dev Container:**

- Open Command Palette (Ctrl+Shift+P)
- Run Task → "Run All CI Checks"
- Or use keyboard shortcuts for individual tasks

### Fixing Issues

**Linting errors:**
```bash
# Auto-fix
make format
```

**Dockerfile warnings:**
```bash
# Check Dockerfiles
make lint-dockerfiles

# Fix based on hadolint suggestions
# Review best practices: https://github.com/hadolint/hadolint
```

**Proto out of sync:**
```bash
# Regenerate protos
make protos

# Commit changes
git add proto/
git commit -m "chore: Regenerate proto files"
```

## Code Style

### Python

We use **ruff** for linting and formatting:
- Line length: 100 characters
- Target version: Python 3.14 (`requires-python = ">=3.14,<3.15"` in `pyproject.toml`)
- Automatic import sorting
- Comprehensive linting rules

Run `make format` to auto-fix most issues.

### Type Hints

- Gradually adopting type hints across the codebase
- New code should include type hints where possible
- Type hints are enforced by ruff

### Dockerfiles

We use **hadolint** for Dockerfile linting:
- Follow best practices for layer caching
- Pin base image versions explicitly
- Minimize layer count

## Project Structure

```
JoustMania/
├── .devcontainer/          # VS Code dev container setup
├── .github/                # GitHub Actions workflows
│   └── workflows/
│       └── ci.yml          # Main CI pipeline
├── docs/                   # Documentation
├── lib/                    # Shared Python libraries
├── proto/                  # Protocol buffer schemas
├── scripts/
│   ├── ci/                 # CI/CD scripts
│   ├── setup/              # Installation scripts
│   └── pairing-daemon/     # PS Move pairing service
├── services/               # Microservices
│   ├── controller_manager/
│   ├── game_coordinator/
│   ├── settings/
│   ├── menu/
│   ├── audio/
│   └── dashboard/          # Web UI + reverse proxy
├── tests/                  # Integration tests
├── tools/                  # Development utilities
└── Makefile                # Build targets
```

## Testing

### Running Tests

```bash
# Unit tests (from each service directory)
cd services/<service-name>
uv run pytest

# Integration tests
make test
```

### Adding Tests

- Place unit tests in `services/<service_name>/tests/`
- Place integration tests in `tests/integration/`
- Follow existing test patterns

## Protobuf Changes

If you modify `.proto` files:

1. Regenerate Python code:
   ```bash
   make protos
   ```

2. Verify bytecode compilation:
   ```bash
   ls -lh proto/__pycache__/*.opt-2.pyc
   ```

3. Commit generated files:
   ```bash
   git add proto/*_pb2.py proto/*_pb2_grpc.py proto/__pycache__/
   git commit -m "chore: Regenerate proto files"
   ```

## Docker Changes

If you modify Dockerfiles:

1. Lint the Dockerfile:
   ```bash
   make ci-lint-dockerfiles
   ```

2. Build the service:
   ```bash
   docker compose build <service-name>
   ```

## Commit Message Guidelines

We follow [Conventional Commits](https://www.conventionalcommits.org/):

- `feat:` - New feature
- `fix:` - Bug fix
- `docs:` - Documentation changes
- `refactor:` - Code refactoring
- `test:` - Adding tests
- `chore:` - Maintenance tasks
- `perf:` - Performance improvements

Examples:
```
feat: Add RSSI tracking for controllers
fix: Resolve memory leak in game loop
docs: Update CONTRIBUTING guide
refactor: Extract common game base class
test: Add unit tests for settings service
chore: Regenerate proto files
```

## Getting Help

- **Documentation**: Check `/docs` directory
- **Issues**: [GitHub Issues](https://github.com/WatchMeJoustMyFlags/JoustMania/issues)
- **Discussions**: [GitHub Discussions](https://github.com/WatchMeJoustMyFlags/JoustMania/discussions)

## Resources

- [Protocol Buffers Guide](https://developers.google.com/protocol-buffers)
- [Docker Best Practices](https://docs.docker.com/develop/dev-best-practices/)
- [Python Type Hints](https://docs.python.org/3/library/typing.html)
- [Ruff Documentation](https://docs.astral.sh/ruff/)
- [Hadolint Documentation](https://github.com/hadolint/hadolint)

---

Thank you for contributing to JoustMania! 🎮
