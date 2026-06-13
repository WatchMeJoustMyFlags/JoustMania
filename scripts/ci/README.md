# CI Scripts

Scripts for complex CI operations that require multi-step logic.

Most CI operations are now inlined in the Makefile. These scripts handle
operations too complex to express cleanly in Make syntax.

## Prerequisites

- Docker installed and running
- Project cloned locally

## Scripts

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
image bakes — the shared baked-deps inputs (root/proto/lib `pyproject.toml` +
`images/builder/**`) plus that service's own `pyproject.toml` + `Dockerfile`.
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
