# JoustMania Makefile
#
# Most Docker operations are done directly with docker compose.
# This Makefile provides shortcuts for common development tasks.
#
# Quick Start:
#   docker compose up -d              # Start with existing images
#   docker compose up -d --build      # Build and start
#   docker compose pull && docker compose up -d  # Pull from GHCR and start
#
# Or use make targets for convenience:
#   make up-mock                      # Start in mock mode (no hardware)
#   make builders                     # Build base images (once)
#   make test                         # Run integration tests

.PHONY: help
help:
	@echo "JoustMania Development Targets"
	@echo "=============================="
	@echo ""
	@echo "Docker (use docker compose directly for most operations):"
	@echo "  make dev             - Start with hot-reload source mounting"
	@echo "  make up-mock         - Start in mock mode (no hardware)"
	@echo "  make up-dynatrace    - Start with Dynatrace telemetry export"
	@echo "  make builders        - Build base images (run once)"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint            - Run linting (ruff)"
	@echo "  make format          - Format code (ruff)"
	@echo "  make check           - Run all checks (lint + format)"
	@echo ""
	@echo "Testing:"
	@echo "  make test            - Run all tests (unit + integration)"
	@echo "  make test-integration - Run integration tests only (CI)"
	@echo "  make test-dev        - Run integration tests with pre-built images (fast)"
	@echo "  make test TEST=name  - Run specific test by name"
	@echo ""
	@echo "Protos:"
	@echo "  make protos          - Generate Python protobuf files"
	@echo "  make protos-all      - Generate all protobuf files (Python, TS, Go)"
	@echo ""
	@echo "Direct docker compose commands:"
	@echo "  docker compose up -d              # Start services"
	@echo "  docker compose up -d --build      # Build and start"
	@echo "  docker compose down               # Stop services"
	@echo "  docker compose logs -f            # Follow logs"
	@echo "  docker compose ps                 # List services"
	@echo "  docker compose pull               # Pull images from GHCR"

# ============================================================================
# Docker Convenience Targets
# ============================================================================

# Hot-reload mode: volume-mounts Python source for live code changes without rebuilds
.PHONY: dev
dev:
	docker compose -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.dev.yml up -d $(if $(BUILD),--build)
	@echo ""
	@echo "=========================================="
	@echo "JoustMania is running (DEV MODE)"
	@echo "=========================================="
	@echo "  Source directories are volume-mounted."
	@echo "  Restart a service after code changes:"
	@echo "    docker compose restart game-coordinator"
	@echo ""
	@echo "  Dashboard:  http://localhost/"
	@echo "  Jaeger:     http://localhost/jaeger/"
	@echo "  Grafana:    http://localhost/grafana/"

# Dynatrace mode: adds parallel telemetry export to Dynatrace alongside local stack
# Requires DYNATRACE_ENDPOINT and DYNATRACE_API_TOKEN in .env or environment
.PHONY: up-dynatrace
up-dynatrace:
	docker compose -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.dynatrace.yml up -d $(if $(BUILD),--build)
	@echo ""
	@echo "=========================================="
	@echo "JoustMania is running (DYNATRACE MODE)"
	@echo "=========================================="
	@echo "  Local stack:     http://localhost/"
	@echo "  Dynatrace:       check your Dynatrace environment"
	@echo ""
	@echo "  Traces, metrics, and logs are exported to both"
	@echo "  the local stack and Dynatrace in parallel."

# Mock mode uses CI flagd config (controller_backend=mock)
.PHONY: up-mock
up-mock:
	docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d $(if $(BUILD),--build)
	@echo ""
	@echo "=========================================="
	@echo "JoustMania is running (MOCK MODE)"
	@echo "=========================================="
	@echo "  Using flagd performance.ci.json (controller_backend=mock)"
	@echo "  Dashboard:  http://localhost/"
	@echo "  Jaeger:     http://localhost/jaeger/"
	@echo "  Prometheus: http://localhost/prometheus/"
	@echo "  Grafana:    http://localhost/grafana/"

# ============================================================================
# Builder Images
# ============================================================================
# Build once, then service builds are much faster.

BUILDER_MARKER := .builder-built
PSMOVE_BUILDER_MARKER := .psmove-builder-built

.PHONY: builders
builders: $(BUILDER_MARKER) $(PSMOVE_BUILDER_MARKER)
	@echo "✓ All builder images ready"

$(BUILDER_MARKER): images/builder/Dockerfile images/builder/requirements-common.txt
	@echo "Building shared Python builder image..."
	docker build -t ghcr.io/watchmejoustmyflags/joustmania/builder:latest images/builder/
	@touch $(BUILDER_MARKER)

$(PSMOVE_BUILDER_MARKER): images/psmove-builder/Dockerfile
	@echo "Building psmoveapi builder image..."
	docker build -t ghcr.io/watchmejoustmyflags/joustmania/psmove-builder:latest images/psmove-builder/
	@touch $(PSMOVE_BUILDER_MARKER)

.PHONY: builders-force
builders-force:
	docker build --no-cache -t ghcr.io/watchmejoustmyflags/joustmania/builder:latest images/builder/
	docker build --no-cache -t ghcr.io/watchmejoustmyflags/joustmania/psmove-builder:latest images/psmove-builder/
	@touch $(BUILDER_MARKER) $(PSMOVE_BUILDER_MARKER)

.PHONY: clean-builders
clean-builders:
	rm -f $(BUILDER_MARKER) $(PSMOVE_BUILDER_MARKER)

# ============================================================================
# Code Quality (using uv directly - fast, no Docker overhead)
# ============================================================================

.PHONY: lint
lint:
	uv run ruff check .

.PHONY: format
format:
	uv run ruff format .

.PHONY: format-check
format-check:
	uv run ruff format --check .

.PHONY: check
check: lint format-check
	@echo "✓ All checks passed"

# ============================================================================
# Protobuf Generation
# ============================================================================

.PHONY: protos
protos:
	@echo "Generating Python protobuf files..."
	bash proto/generate_proto.sh

.PHONY: protos-ts
protos-ts:
	@echo "Generating TypeScript protobuf files..."
	cd proto && buf generate --template buf.gen.yaml

.PHONY: protos-go
protos-go:
	@echo "Generating Go protobuf files..."
	cd proto && buf generate --template buf.gen.go.yaml

.PHONY: protos-all
protos-all: protos protos-ts protos-go
	@echo "✓ All protobuf files generated"

.PHONY: clean-protos
clean-protos:
	rm -f proto/*_pb2.py proto/*_pb2_grpc.py
	rm -rf proto/__pycache__
	rm -rf services/connect-proxy/gen/*

# ============================================================================
# Testing
# ============================================================================
# Uses a separate venv (.venv-test) to avoid conflicts with Docker-created files.

# Test environment setup
TEST_VENV := .venv-test
TEST_ENV := UV_PROJECT_ENVIRONMENT=$(TEST_VENV)

# Clean test venv if it has wrong permissions (Docker root ownership issue)
.PHONY: clean-test-venv
clean-test-venv:
	@if [ -d "$(TEST_VENV)" ] && [ ! -w "$(TEST_VENV)" ]; then \
		echo "Removing $(TEST_VENV) (permission issue)..."; \
		sudo rm -rf $(TEST_VENV); \
	fi

# Run all tests (unit + integration) - requires system deps for audio (libasound2-dev)
.PHONY: test
test: clean-test-venv
	uv run --all-packages pytest $(if $(TEST),-k "$(TEST)")

# Integration tests only (used by CI - unit tests run separately in service-checks)
.PHONY: test-integration
test-integration: clean-test-venv
	$(TEST_ENV) uv run --package joustmania-integration-tests \
		pytest tests/integration/ -v $(if $(TEST),-k "$(TEST)")

# Run with prebuilt images from GHCR instead of building
.PHONY: test-pulled
test-pulled: clean-test-venv
	USE_PREBUILT_IMAGES=true IMAGE_TAG=$(or $(IMAGE_TAG),latest) \
		$(TEST_ENV) uv run --package joustmania-integration-tests \
		pytest tests/integration/ -v $(if $(TEST),-k "$(TEST)")

# Integration tests with pre-built images + volume-mounted source (no rebuild)
# Requires images: run `docker compose pull` or `make dev` first
.PHONY: test-dev
test-dev: clean-test-venv
	USE_PREBUILT_IMAGES=true USE_DEV_MOUNTS=true $(TEST_ENV) uv run --package joustmania-integration-tests \
		pytest tests/integration/ -v $(if $(TEST),-k "$(TEST)")

# Pause before teardown for Jaeger inspection
.PHONY: test-debug
test-debug: clean-test-venv
	PAUSE_BEFORE_TEARDOWN=1 $(TEST_ENV) uv run --package joustmania-integration-tests \
		pytest tests/integration/ -v -s $(if $(TEST),-k "$(TEST)")

# ============================================================================
# CI Targets (used by GitHub Actions)
# ============================================================================
# These are optimized for CI - local development should use targets above.

# Build CI proto image (used by validation scripts)
.PHONY: ci-proto-image
ci-proto-image:
	docker build -t joustmania/ci-proto:latest tools/ci-proto/

# Validate proto files match generated code
.PHONY: ci-validate-protos
ci-validate-protos: ci-proto-image
	bash scripts/ci/validate-protos.sh

# Validate Python package dependencies
.PHONY: ci-validate-packages
ci-validate-packages: ci-proto-image
	bash scripts/ci/validate-packages.sh

# Lint Dockerfiles (CI uses hadolint container)
.PHONY: ci-lint-dockerfiles
ci-lint-dockerfiles:
	docker run --rm -v "$(PWD):/workspace:ro" -w /workspace \
		hadolint/hadolint:latest-alpine \
		sh -c 'find . -name "Dockerfile" -type f -exec hadolint {} \;'
