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

### Configuration: flags, not environment variables (C-035)

Anything an operator may want to change while the system runs is a **flagd flag** (declared variants, read at use time,
a row in [flags.md](specs/flags.md) first), never a new environment variable: env vars need a restart and can't be
changed on stage. Env vars are only for bootstrap values needed before flagd is reachable (hosts, ports, URLs), secrets,
OTEL/deploy identity, the flag service's own config, startup-only values that cannot be read from flagd (justified in
flags.md §15.2), and agent safety ceilings, where the env var is the ceiling and a
flag may only tighten it ([C-034](requirements.md)). Reviewers flag a new env-only tunable.

### The spec tree

| Layer | File(s) | AIUP skill | Holds |
|---|---|---|---|
| Vision | [vision.md](vision.md) (+ [NORTHSTAR.md](NORTHSTAR.md)) | (none) | Why the product exists, stakeholders, northstar constraints |
| Requirements | [requirements.md](requirements.md) | `requirements` | `FR-xxx` user stories, `NFR-xxx` measurable qualities, `C-xxx` constraints |
| Entity model | [entity_model.md](entity_model.md) | `entity-model` | Domain entities, ER diagram, 5-column attribute tables |
| Use cases | [use_cases.puml](use_cases.puml), [use_cases/UC-xxx.md](use_cases/) | `use-case-diagram`, `use-case-spec` | Actor goals, main success scenario, alternative flows, business rules |
| Technical specs | [specs/services.md](specs/services.md), [specs/flags.md](specs/flags.md), [specs/telemetry.md](specs/telemetry.md), [specs/decide-contract.md](specs/decide-contract.md) | (repo convention) | Ports, RPCs, flag catalog, span / metric / attribute names, the decision contract |

The AIUP skills come from the stack-agnostic `aiup-core` plugin, enabled for everyone
at project scope in `.claude/settings.json` (#1259, #1263; marketplace
[`AI-Unified-Process/marketplace`](https://github.com/AI-Unified-Process/marketplace),
the successor of the archived `martinellich/aiup-marketplace`; auto-update off, so a
plugin update is a reviewed PR). The skills we use: `requirements`, `entity-model`,
`use-case-diagram`, `use-case-spec`, `spec-review` (cross-file lint, trace matrix,
HTML report) and `test-case` (end-to-end test case documents). The plugin also ships
`business-process` (BPMN) and `reverse-engineer`, which JoustMania does not use. The
stack-specific plugins of that marketplace (Vaadin/jOOQ, Angular/JPA, ...) are not
enabled. The plugin also brings the Context7 MCP server (`plugin:aiup-core:context7`),
which stays enabled by maintainer decision; disable it per user if you do not want it.
To opt out of the whole plugin for yourself, set
`"enabledPlugins": {"aiup-core@ai-unified-process-marketplace": false}` in your
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
   the same `// UC:` line above `#[test] fn …` in Rust, `describe('UC-040: …')` in the dashboard (see [.claude/skills/README.md](../.claude/skills/README.md)).
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
tagged test (see the `/coverage-check` skill, added by #1289) and no blocking `Known gap` line is open. A stated `Known gap` on a precondition, a postcondition, an NFR or a clause that only an e2e or hardware run can assert does not block `Tested`; a `Known gap` on a main step, an alternative flow or a BR does. (maintainer rule, #1725). A use case's status is never above the weakest FR on its
`Requirements:` line; FRs that only a planned flow needs go on a separate `Planned:` line with their
issue number, so the use case can be `Implemented` while they stay `Open`. The flows themselves go under a
`## Planned extensions` section (IDs kept, issue and status Open); they do not count toward `Tested` until they
move back under `## Alternative Flows` with their code. Conversely, an FR's status is
derived from its use cases: it is `Verified` only when every use case that lists it under `Requirements:` is
`Tested`, otherwise at most `Implemented`. NFRs and constraints are exempt from both rules: NFRs are measured separately, constraints are verified by
review and the technical specs; only FRs derive their status from their use cases.

When a flow or business rule leaves a use case, keep its ID: a flow that moved to another use case is
titled `(moved)`, one that no longer exists `(retired)`. Its trigger says where it went (or why it
ended) and that the ID is kept so it is not reused, and its body is one neutral "No steps" line.
See UC-030 A2 and UC-035 A2.

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
(e.g. `none: refactor, no behaviour change`) and add the `no-spec-change` label
(see the CI gate below). If you are unsure whether
something is observable, it probably is: update the spec.

### CI spec gate

The `Spec Gate` job in `ci.yml` (#1263, required through `CI Complete`) has two steps:

1. **Spec lint** (runs when `docs/**`, `.github/**` or `scripts/aiup/**` change):
   `scripts/aiup/spec-review/scripts/spec_lint.py`, the `spec-review` skill's linter,
   vendored from the AIUP marketplace (Apache-2.0, see `scripts/aiup/NOTICE`; three
   local patches: the requirement Status column is read by its header because our
   tables have a Notes column after it; FRs on a use case's `**Planned:**` line
   count as covered for `FR_UNCOVERED` without feeding the derived status checked by
   `REQ_STATUS_DRIFT`; and `REQ_STATUS_DRIFT` checks FRs only, since NFRs are
   measured separately and constraints are verified by review and the technical specs). `scripts/aiup/spec-lint-gate.sh` runs it over `docs/`:
   - **Every error fails**: `DUPLICATE_ID`, `SPEC_MISSING`, `NOT_IN_DIAGRAM`,
     `DANGLING_REF`, and use case files the AIUP format cannot parse. Errors are
     checked without the baseline, so they cannot be baselined.
   - **New warnings fail** (`REQ_STATUS_DRIFT`, `FLOW_TERMINATION`, `WEAK_WORD`, ...).
     The existing ones are accepted in `scripts/aiup/spec-lint-baseline.json`.
   - **The baseline only shrinks**: on a PR it may not have more entries than on
     `main`, unless the PR carries the `spec-baseline-growth` label (a deliberate,
     reviewed acceptance of new warnings).

   Run the same gate locally before pushing a docs change:

   ```bash
   scripts/aiup/spec-lint-gate.sh            # errors + new warnings
   BASE_REF=main PR_LABELS= scripts/aiup/spec-lint-gate.sh   # + baseline growth
   ```

   Fixing a baselined finding is reported as `INFO BASELINE_STALE` (not a failure);
   refresh the baseline in the same PR so it shrinks:
   `python3 scripts/aiup/spec-review/scripts/spec_lint.py --docs docs --baseline scripts/aiup/spec-lint-baseline.json --update-baseline`.
2. **Spec touched** (pull requests changing `services/**`, `lib/**` or `proto/**`;
   tests (`tests/`, `testutil/`, `mocks/`, `conftest.py`, `*_test.*`, `test_*.py`,
   `*.test.ts(x)`, `*.spec.ts(x)`), `*.md`, dependency manifests/locks and
   Dockerfiles do not count; Grafana dashboards and collector
   config do, since `docs/specs/telemetry.md` documents them): the PR must also change
   `docs/requirements.md`, `docs/use_cases/**`, `docs/use_cases.puml`, `docs/specs/**`,
   `docs/specs-inventory.md` or `docs/entity_model.md`. A change to
   the baseline flag files `services/flagd/*.json` must touch `docs/specs/flags.md`, a change to
   `proto/*.proto` must touch `docs/specs/services.md`. Exempt: PRs with the
   `no-spec-change` label; Renovate, Dependabot and release-please PRs (the bot
   accounts `renovate[bot]`, `dependabot[bot]`, `github-actions[bot]`, matched by
   login and account type); and the agent's experiment-promoter PRs (branch
   `agent/experiment-*` in this repository, title `Flag experiment: ...`).

Adding or removing `no-spec-change` or `spec-baseline-growth` re-runs the Spec Gate
(and, for `spec-baseline-growth`, the Telemetry Contract job) automatically (`spec-label-rerun.yml`).

### Spec references

`make spec-refs-check` (Telemetry Contract job, #1280) checks every `path:line` and `path:Symbol` in `docs/specs/*.md` (not `generated/`, not `notes/`, not fenced code blocks):

- the path must be a full repo path (`services/agent/decide/state.go:digest`, never `state.go:digest`) to an existing file; the line must be within the file; the symbol (each dotted part) must occur as a word somewhere in the file (a word search, not a declaration check). These fail every PR, including code-only ones that delete or rename what a spec cites. Fix the spec line; `scripts/spec_refs.py --suggest` prints a ready `path:Symbol` for every line ref, and the error for an out-of-range line names one.
- prefer `path:Symbol`: it survives edits. New `path:line` refs are allowed but a growth in their total versus the base branch raises a `::warning` with suggestions.
- every Go `Span*` / `Attr*` constant value must appear in `telemetry.md` as its exact key. Undocumented ones sit in `docs/specs/notes/spec-refs-baseline.json`; the baseline may only grow with the `spec-baseline-growth` label, and a stale entry (now documented or gone) fails: run `make spec-refs` to prune it.
- an example that is not a real reference: put `<!-- spec-refs: ignore -->` on its line.
- drift (#1918): `make spec-refs-report` (`scripts/spec_refs.py --report`) lists broken refs and line refs whose cited lines changed after the spec line was last edited. The weekly `spec-refs-drift.yml` workflow runs it and opens or updates ONE issue titled `Spec reference drift report` (label `documentation`); it never gates a PR.

A reference fix is not a spec change: it satisfies the "spec touched" check mechanically, but does not replace the `no-spec-change` reasoning for a behaviour change.

**Labels and CI (#1527).** Adding or removing a label never starts a `CI` run.
GitHub takes the required `CI Complete` check from the newest run of the `CI`
workflow on the head SHA, so a label-only run, which tests nothing, would block
the PR. Labels can be added at any time after `gh pr create`. The one label that
changes CI is `ci-full` (build every image, run every check). Adding it later
re-runs the PR's latest CI run in place through `ci-full-label.yml`. That run
reads the labels live, so the re-run is a full one, and its `CI Complete` is the
one that counts. If the run is still in progress and started before the label,
the workflow waits for it to finish and then re-runs it.

On a **fork PR** neither label workflow has permission to re-run jobs: push a
commit (an empty one is fine) after adding the label instead.

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
git worktree add .worktrees/issue-<NUMBER> -b fix/<description> origin/main

# Work in the isolated directory
cd .worktrees/issue-<NUMBER>

# After PR is merged, clean up
git worktree remove .worktrees/issue-<NUMBER>
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
   make check
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
7. **Spec Gate** - Spec lint over `docs/` and the spec-touched check (see [CI spec gate](#ci-spec-gate))
8. **Spec references** - `make spec-refs-check` in the same job (see [Spec references](#spec-references))
9. **Generated specs** - `docs/specs/generated/` (metric catalog, RPC tables, flag sets, `ci/` flag diffs) must match the code: run `make specs` and commit (checked by `make specs-check` in the Telemetry Contract job). Totals (flags, RPCs, metrics) are not committed, so parallel PRs do not conflict on them (#2152): `make specs-counts` prints them. After a rebase that conflicts in a generated file, take either side and run `make specs`

**When the full-stack e2e job runs (#1583).** `Integration Tests` (one job, one
compose stack, about 5-8 minutes) starts the whole docker-compose stack, so it
gates the merge queue rather than every PR push:

| Event | e2e |
|---|---|
| PR push | skipped (lint, unit, contract, service tier and builds still run) |
| PR with the `ci-full` label | run |
| PR whose non-docs diff touches `docker-compose*.yml`, any `Dockerfile*`, `tests/integration/**`, `services/otel-collector/**`, `images/otel-collector/**`, `services/envoy/**`, `services/flagd/**` or `.github/workflows/ci.yml` | run |
| `merge_group` (merge queue) | always run, with every stack image built at HEAD (no `:latest` fast path); `CI Complete` fails unless it succeeds |
| push to `main` | skipped: the queue already tested that commit; images build and publish without waiting for them |
| `workflow_dispatch` | run |

To force them on a PR, add `ci-full` (it also builds every image). A skipped e2e job
keeps `CI Complete` green only where it was not required (`e2e_required`
output of the `changes` job).

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

# Validate protos (Python stubs; agent Go stubs in services/agent/gen, #1801)
make ci-validate-protos
make ci-validate-agent-protos
```

### PR gates and merging (`make pr-gates`, `safe-merge.sh`)

`make pr-gates` (`scripts/ci/local-gates.sh`) runs the local gates for what the branch changed (files vs
`origin/main`, plus uncommitted and untracked files). It always runs `make lint` and the spec lint gate, then
selects from the diff: per-service pytest (lib or proto changes fan out to every Python service, as in CI),
`go vet` + `go test -race` for the agent and flag service, the connect-proxy Docker builder stage (the only
place its generated code exists), the agent Go stub drift check (`make ci-validate-agent-protos`, on proto or
`services/agent/gen` changes), dashboard `tsc --noEmit` + vitest (Node 24 from `~/.nvm`), the telemetry
contract, the flagd contract (flag files or the flag-service registry), Grafana dashboard JSON, compose
`config -q` on the main file combinations and a YAML duplicate-key check.

The service tier (T2, `make test-service`) is deliberately a subset locally:

| Diff touches | Local service tier |
|---|---|
| harness / stack: non-`test_*.py` files under `tests/service/` (not `README.md`), `tests/contract/{flagd_harness,infra_config}.py`, `tests/integration/{helpers,flag_service}.py`, `tests/integration/fake_openai/**`, `docker-compose.yml`, `services/flagd/**`, `services/flag-service/**`, `proto/**`, `pyproject.toml`, `uv.lock`, the `server.py` of game_coordinator / menu / controller_manager | full (`service`) |
| only `tests/service/**/test_*.py` (existing files) | those files (`service-touched`: `make test-service SERVICE_TESTS="<files>"`) |
| anything else (lib, other service code, agent, rust-hid, `Makefile`, `ci.yml`) | none; the summary prints a note |

PR CI "Service Tests" runs the full tier for the CI path filter (it can be path-skipped on a PR that touches
none of them) and the merge queue always runs it. A local PASS is therefore not tier coverage for product paths;
reviewers require "Service Tests" = pass when those paths changed.

```bash
make pr-gates                           # auto-detect
make pr-gates SERVICE=menu,agent        # explicit gate keys (see the script header)
scripts/ci/local-gates.sh --plan        # list the selected gates, run nothing
```

It prints one `PASS`/`FAIL`/`SKIP` line per gate with its duration, then the output of failing gates only,
and exits non-zero on any failure. CI stays the source of truth; the mapping is narrower than CI's filters (the service tier most of all, see above).

`scripts/ci/safe-merge.sh <PR> <HEAD_PREFIX>` waits for CI (polling every 75 s with backoff, never
`gh run watch`), ignores SonarCloud and checks of cancelled runs superseded by a newer run on the same head,
and squash-merges only at the expected head (`--match-head-commit`). `--close N,N [--comment-file F]` (default comment "Done in #<PR>: <PR title>") and
`--note-issue N --note-file F` act only once the PR reads `MERGED` (`{MERGE}` becomes the short merge SHA).
`--wait-ci` waits and reports without merging; on red it names known infrastructure failures (stale
collector image, fast-path stale prebuilt images, `blob not found`) with "add `ci-full`" or "rerun" advice.
`--rerun-failed-once` reruns the failed jobs of ANY red run once (real failures included) before giving
up; a failure that survives the rerun is not infrastructure. A CONFLICTING PR fails fast with exit 5
(GitHub runs no CI on it: rebase). A newest run cancelled before its matrix expanded (a CANCELLED job's
name still shows `${{ ... }}`, nothing failed) is reported as `infra=cancelled-run`, and `--rerun-failed-once` reruns the
whole run. When `main` requires a merge queue (#1555), `gh pr merge` enqueues instead of merging: the
script reports `QUEUED` (exit 6) without touching issues, and with `--wait-queue` it waits for the queue's
`merge_group` run and ends in the normal `MERGED` path or `EJECTED` (exit 7). Agents hand this step to the `pr-merger` agent (`.claude/agents/pr-merger.md`) after a review by the `pr-reviewer` agent (`.claude/agents/pr-reviewer.md`, workflow `.claude/skills/review-pr`). Both scripts have
tests in `scripts/tests/`.

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
make ci-lint-dockerfiles

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
- Line length: 120 characters
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
