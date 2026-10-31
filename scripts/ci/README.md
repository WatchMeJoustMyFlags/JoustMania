# CI Scripts

Scripts for complex CI operations that require multi-step logic.

Most CI operations are now inlined in the Makefile. These scripts handle
operations too complex to express cleanly in Make syntax.

## Prerequisites

- Docker installed and running
- Project cloned locally

## Scripts

### `local-gates.sh` (`make pr-gates`)

Local PR gates selected from the changed files; see the script header and
docs/CONTRIBUTING.md "PR gates and merging". Test: `scripts/tests/test-local-gates.sh`.
`yaml-dupkeys.py` is its YAML duplicate-key check.

### `gate-run.sh`

Validated runner for the gate-runner agent (#1565): checks the worktree, the log
directory and the command against an allow-list in code, runs it N times without
a shell, writes logs outside the worktree and prints a compact verdict
(PASS / FAIL / FLAKY k/N, mutation-check verdicts with `--expect-fail`). Usage
and the allow-list in the script header. Test: `scripts/tests/test-gate-run.sh`.
Detach / await (#2036): the run is a detached job (`detach.sh`) under
`<logs>/<worktree>-<head8>/<name>.job/`; one call waits at most `GATE_RUN_WAIT` s
(default 540). Exit 0 expected verdict, 1 other verdict (also `ERROR: killed`),
2 REFUSED, 3 `VERDICT: PENDING (<n>s); rerun the same command`: the same command
again attaches to the job. A finished job with another command or worktree state,
and a killed job, are replaced by a fresh one.

Threat model: guards against agent and prompt mistakes (what the caller adds to a
command), not a sandbox for untrusted PR code. Every allowed form runs the
worktree's own code (Makefile recipes, pytest conftest/plugins, go test binaries,
cargo build.rs, scripts/tests). Run it only on PRs you would run locally.
Accepted risk: pytest `-p`/`-o`/`--rootdir`/`--import-mode` and go
`-ldflags`/`-gcflags` pass validation but can only reach worktree or venv code,
because absolute paths and `..` are refused; no per-option allow-list.

### `detach.sh`

Detach-and-await for long commands (#2036): `detach.sh --dir <abs job dir> [--wait <s>] -- <argv>`
starts the command once as a detached job (setsid, own session) and waits at most
`--wait` s (0-580, default 540) per call, so an agent stays in foreground Bash calls.
The same argv again attaches to the job or prints its result (never a second job);
a different argv for the dir is REFUSED (exit 2). Exit: the command's own code, or
3 with a last line `VERDICT: PENDING (<n>s); rerun the same command`, or 1 with
`VERDICT: ERROR: killed` (never restarted; use a new `--dir`). A wrapped command
may exit 3 itself, so loop only on exit 3 AND a `VERDICT: PENDING` last line. Used
by `gate-run.sh` and the pr-merger agent. Test: `scripts/tests/test-detach.sh`.

### `safe-merge.sh`

Gentle CI wait + exact-head squash merge + post-merge issue close/comment, or
`--wait-ci` to report only; `--arm` / `--revoke` record or withdraw the `Reviewed head` status (#1809);
`--rearm` (merge-sweep) re-arms a still-reviewed SHA without posting.
Usage in the script header. Test: `scripts/tests/test-safe-merge.sh`.

### `merge-sweep.sh`

One pass over our PRs in the review-gated merge flow (#1809): dequeues UNMERGEABLE merge-queue entries,
re-arms a dequeued or ejected PR (`safe-merge.sh --rearm`, which posts no status and re-checks it right before
arming) at most once per head and only when the newest `Reviewed head` status on its head is success by an
approver, and posts the `## Merge notes` close/note lines of recently merged PRs (targets: the `Part of #N`
issues only). Never touches PRs by other authors; one sweep at a time (flock); `--dry-run` changes nothing.
Approvers: `REVIEW_APPROVERS` from the env, else the repo variable of that name (`gh variable get`), else aepfli,
the same list `review-gate.yml` reads from `vars.REVIEW_APPROVERS`; a local override must match it. Run by the pr-merger
agent's sweep mode. Usage in the script header. Test: `scripts/tests/test-merge-sweep.sh`.

### `validate-protos.sh`

Validate protobuf file generation and bytecode compilation.

- Generates proto files in Docker container
- Checks for uncommitted changes via git diff
- Verifies bytecode compilation (.opt-2.pyc files)

```bash
make ci-validate-protos  # Recommended
# or
bash scripts/ci/validate-protos.sh
```

### `validate-packages.sh`

Validate Python package installation and imports.

- Installs all workspace packages with uv
- Tests proto imports work correctly
- Checks for dependency conflicts

```bash
make ci-validate-packages  # Recommended
# or
bash scripts/ci/validate-packages.sh
```

### `deps-fingerprint.sh` (#925)

Print a stable 16-char hash of the files that determine what a given service
image bakes — the shared baked-deps inputs (root/proto/lib `pyproject.toml`, root `uv.lock`)
plus that service's own `pyproject.toml` + `Dockerfile`.
CI computes this **per service** and bakes it into that Python service image as
the OCI label `org.joustmania.deps-fingerprint` (build-arg `DEPS_FINGERPRINT`).

```bash
bash scripts/ci/deps-fingerprint.sh <service>   # e.g. menu, game_coordinator
```

### `check-image-freshness.sh` (#925)

Fast-path freshness guard. Before the integration fast path mounts current
source into pulled `:latest` images, this compares each image's baked
`org.joustmania.deps-fingerprint` label against the current checkout and fails
loudly with a one-line diagnosis on mismatch (the stale-image cascade), instead
of silently mounting incompatible source.

```bash
IMAGE_PREFIX=ghcr.io/watchmejoustmyflags/joustmania IMAGE_TAG=latest \
  bash scripts/ci/check-image-freshness.sh
```

### `check-collector-config.sh` (#1487)

Fast-path collector guard. Runs the pulled otel-collector image's
`validate --config` against the checked-out `services/otel-collector/config.yaml`.
On a mismatch (exit 1) or a failed pull (exit 2) the integration job builds the
collector at HEAD under the same ref; `docker-compose.dev.yml` gives the
collector `pull_policy: missing`, so the suite's `compose pull` keeps that build.

```bash
bash scripts/ci/check-collector-config.sh ghcr.io/<owner>/<repo>/otel-collector:latest
```

### `check-retag-source.sh` (#1487)

Retag guard for `retag-skipped`. Finds the last commit touching the image's
sources (the same globs as its `changes` filter in `ci.yml`) and refuses
(exit 1) when `:main` was built before it: by the build jobs'
`org.opencontainers.image.revision` label, or by the image creation time for
images built before the label. Exit 2 = `:main` missing. Needs full history.
Test: `scripts/tests/test-check-retag-source.sh`.

```bash
IMAGE_PREFIX=ghcr.io/<owner>/<repo> bash scripts/ci/check-retag-source.sh otel-collector
bash scripts/ci/check-retag-source.sh --paths menu-service   # print the source globs
```

### `check-main-tag-order.sh` (#1793)

Publish guard for `docker-build-merge` / `docker-build-extra-merge`. Main runs
are not serialised, so an older run finishing late could move `:main` and
`:latest` back to stale content. The script reads the revision label of the
current `:main` (amd64 config of the index; shared reader in
`lib-image-config.sh`) and skips the moving tags (exit 3) only when it is
proven that `:main` is newer: the run's SHA is an ancestor of the `:main`
revision (fetching `origin/main` first when the revision is not in the
checkout). Missing or unlabelled `:main`, an equal SHA (workflow_dispatch
rerun) or an ancestor move the tags (exit 0); a revision that is unknown after
the fetch or unrelated to the run's SHA is not on main, so the tags move with a
`::warning::`. A failed `git fetch` exits 2 and fails the step. All git calls
use `REPO_DIR` (default `$GITHUB_WORKSPACE`), because the publish step runs in
the digests dir; the jobs check out a blobless partial clone with only
`scripts/ci`. On skip the workflow pushes only `:<sha>` and logs a notice.
`release.yml` is unaffected (it consumes `:<sha>`). Complements
`check-retag-source.sh`, which stays the guard for the retag path.
Test: `scripts/tests/test-check-main-tag-order.sh`.

### `release-retag.sh` (#2338)

`release.yml` tag-images, one matrix job per image: retags `IMAGE:<sha>` (the
release tag commit) to `IMAGE:<version>`. release-please runs from the CI
`workflow_run` of an earlier commit, so the tag commit's own CI is often still
queued: the script polls for `IMAGE:<sha>` with exponential backoff (20 s
doubling, capped at 300 s) for at most `WAIT_TIMEOUT` (2700 s = 45 min), then
copies by digest and verifies the new tag. An existing `:<version>` with another
digest is never overwritten; any inspect error other than a registry "not
found" fails closed at once. On timeout the error names the image and the retag
command (`gh workflow run release.yml -f version=<v> -f sha=<sha>`).
Test: `scripts/tests/test-release-retag.sh`.

```bash
IMAGE_PREFIX=ghcr.io/<owner>/<repo> VERSION=0.9.1 SHA=<40-hex> bash scripts/ci/release-retag.sh menu-service
```

## Makefile Targets

All CI operations are available as Make targets:

```bash
# Code Quality
make lint                  # Run ruff linting
make format                # Format code with ruff
make format-check          # Check formatting without modifying
make check                 # Run lint + type check

# Validation (uses these scripts)
make ci-validate-protos    # Validate proto generation
make ci-validate-packages  # Validate package installation
make ci-lint-dockerfiles   # Lint Dockerfiles with hadolint

# Testing
make test                  # Run integration tests
make test-integration      # Run integration tests (alias)

# Building
make ci-build-service SERVICE=<name>  # Build single service
```

## Local Development

```bash
# Format code before committing
make format

# Run pre-commit checks
make lint
```

### `prune-collector-tags.sh`

Weekly GHCR retention (#1787, workflow `prune-collector-tags.yml`) for the
`otel-collector:<16-hex hash>` tags: deletes hash-only versions beyond the newest
`KEEP` (default 5), except the hash HEAD produces and hashes referenced by the
latest release's `.env` / compose files (belt and braces: release pins use
`IMAGE_TAG` = X.Y.Z or sha, which are separate GHCR versions and never
candidates). Never touches X.Y.Z, `latest`, `main`, `<sha>` or untagged
versions. `KEEP` must be >= 1. `DRY_RUN=1` (workflow_dispatch default) only lists.
The scheduled run deletes for real only when the repo variable
`PRUNE_COLLECTOR_DELETE` is `true` (maintainer sets it); otherwise it is a dry
run. The job runs only on `WatchMeJoustMyFlags/JoustMania`.
Known limit: a hash-only version whose digest is also an untagged child of a
release index would be deleted and break that index's amd64 leg (not observed).
Test: `scripts/tests/test-prune-collector-tags.sh` (fake `gh`).
