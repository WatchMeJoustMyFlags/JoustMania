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
