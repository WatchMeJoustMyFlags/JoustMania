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
#   make test                         # Run integration tests

.PHONY: help
help:
	@echo "JoustMania Development Targets"
	@echo "=============================="
	@echo ""
	@echo "Docker (use docker compose directly for most operations):"
	@echo "  make dev             - Start with hot-reload source mounting"
	@echo "  make up-mock         - Start in mock mode (no hardware)"
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

# Mock mode uses CI flagd config (controller domain: backend=mock)
.PHONY: up-mock
up-mock:
	docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d $(if $(BUILD),--build)
	@echo ""
	@echo "=========================================="
	@echo "JoustMania is running (MOCK MODE)"
	@echo "=========================================="
	@echo "  Using flagd ci/ dir (backend=mock)"
	@echo "  Dashboard:  http://localhost/"
	@echo "  Jaeger:     http://localhost/jaeger/"
	@echo "  Prometheus: http://localhost/prometheus/"
	@echo "  Grafana:    http://localhost/grafana/"

# M8 agent shadow-game dry run (#999). Brings up a RUNNABLE experiment cohort
# loop out-of-the-box: latest images (not the stale release IMAGE_TAG=0.7.0 from
# .env), mock controllers + full observability (ci flag dir), the agent & the
# dashboard profiles, the experiment loop enabled, and one seeded experiment.
# The agent and flagd share the SAME flag dir (services/flagd/ci) so the agent's
# game.json targeting writes actually reach the running flagd (the #999 dir-
# mismatch fix lives in docker-compose.dry-run.yml).
#
# Gating chain (all must be satisfied for the loop to ACT):
#   1. flagd `experiments_enabled` flag = on  -> the LIVE gate post-#1044. It
#      OVERRIDES the AGENT_EXPERIMENTS_ENABLED env, which is now dead config for
#      this purpose. Fail-closed `off` by default; MANUAL opt-in via the enable
#      step below.
#   2. agent.json `enabled` = on       -> master kill-switch, fail-closed `off`
#      by default. Without it the loop self-aborts:
#      `experiment.torn_down ... reason="kill-switch: agent disabled"`.
#      (The enable step below flips BOTH 1 and 2 in one command.)
#   3. code_improvement.* promotion gates -> remain default (promotion stays off)
# We do NOT mutate the committed flag files here; the operator runs the enable
# step, which flips the ci/ flag dir at runtime only.
.PHONY: dry-run
dry-run:
	IMAGE_TAG=$(or $(IMAGE_TAG),latest) docker compose \
		-f docker-compose.yml \
		-f docker-compose.override.yml \
		-f docker-compose.ci.yml \
		-f docker-compose.dry-run.yml \
		--profile agent --profile dashboard up -d $(if $(BUILD),--build)
	@echo ""
	@echo "=========================================="
	@echo "JoustMania M8 AGENT DRY RUN is running"
	@echo "=========================================="
	@echo "  Images:        IMAGE_TAG=$(or $(IMAGE_TAG),latest) (current code, not the .env release pin)"
	@echo "  Controllers:   mock (flagd ci/ dir, backend=mock)"
	@echo "  Flag dir:      services/flagd/ci  (shared rw by agent + flagd -> writes are effective)"
	@echo ""
	@echo "  Experiment loop gating:"
	@echo "    [!] flagd experiments_enabled flag is the LIVE gate (post-#1044) and"
	@echo "        OVERRIDES AGENT_EXPERIMENTS_ENABLED env. Fail-closed off by default."
	@echo "    [x] seeded experiment: windows = frantic music pacing (objective=balanced, N=8/arm)"
	@echo "    [ ] ENABLE experiments  -> ONE step (flips enabled + experiments_enabled on):"
	@echo "        ./scripts/agent-dryrun-enable.sh on   # ci/agent.json: enabled + experiments_enabled -> on"
	@echo "        # gate flips take effect LIVE (~1s, self-gated per-tick since #1044) -> NO agent restart"
	@echo "        # add AGENT_INFERENCE_BACKEND=openai to also flip mode -> llm; setting AGENT_INFERENCE_*"
	@echo "        #   env needs an agent recreate (read at process start): ... up -d agent"
	@echo "        # restore defaults afterwards: ./scripts/agent-dryrun-enable.sh off"
	@echo "    [ ] code_improvement.* promotion gates stay OFF (no real-default promotion)"
	@echo ""
	@echo "  Observability:"
	@echo "    Dashboard:  http://localhost/"
	@echo "    Jaeger:     http://localhost/jaeger/    (agent decision-audit spans)"
	@echo "    Prometheus: http://localhost/prometheus/"
	@echo "    Grafana:    http://localhost/grafana/"
	@echo ""
	@echo "  Runbook: docs/agent-dry-run-runbook.md"
	@echo "  Tear down: docker compose -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.ci.yml -f docker-compose.dry-run.yml --profile agent --profile dashboard down"

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

.PHONY: protos-agent
protos-agent:
	@echo "Generating agent-local Go stubs (committed under services/agent/gen)..."
	# The agent service needs grpc-go clients to drive shadow games (#778). Unlike
	# the connect-proxy gen (build-time, never committed), these stubs are
	# committed INTO the agent module so `go test`/the agent Dockerfile build with
	# no buf step. Uses Docker buf (no local buf required), then relocates the
	# go_package-pathed output into services/agent/gen and fixes ownership.
	rm -rf /tmp/joust-agent-gen && mkdir -p /tmp/joust-agent-gen
	docker run --rm \
		-v "$(CURDIR)/proto:/workspace/proto" \
		-v "/tmp/joust-agent-gen:/out" \
		-w /workspace/proto \
		bufbuild/buf:1.47.2 generate --template buf.gen.agent.yaml \
		--path controller_manager_mock.proto --path game_coordinator.proto
	rm -rf services/agent/gen && mkdir -p services/agent/gen
	docker run --rm -v /tmp/joust-agent-gen:/in -v "$(CURDIR)/services/agent/gen:/dest" alpine:3.19 \
		sh -c 'cp -r /in/github.com/joustmania/agent/gen/* /dest/ && chown -R $(shell id -u):$(shell id -g) /dest'
	@echo "✓ Agent Go stubs generated"

.PHONY: protos-all
protos-all: protos protos-ts protos-go protos-agent
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
# PYTEST_EXTRA: optional extra pytest args (e.g. CI passes "--reruns 1" via
# pytest-rerunfailures so a single integration flake doesn't skip image publish
# and stale :latest — the #925 cascade). Generic: reruns ANY failing test once,
# does not target tests by name.
# CAVEAT: the integration docker_compose fixture is session-scoped and some state
# is process-global (the rate limiter, interventions_allowed), so a test-isolation
# bug can fail first then pass on rerun — masked as a flake. The rerun is a safety
# net, NOT the fix; the freshness guard (check-image-freshness.sh) is what actually
# prevents the stale-image cascade.
.PHONY: test-integration
test-integration: clean-test-venv
	$(TEST_ENV) uv run --package joustmania-integration-tests \
		pytest tests/integration/ -v $(PYTEST_EXTRA) $(if $(TEST),-k "$(TEST)")

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
