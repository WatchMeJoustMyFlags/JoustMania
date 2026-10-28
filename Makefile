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
	@echo "  make pr-gates        - Local PR gates for what changed (SERVICE=menu,agent to pick)"
	@echo ""
	@echo "Testing:"
	@echo "  make test            - Run all tests (unit + integration)"
	@echo "  make test-integration - Run integration tests only (CI)"
	@echo "  make test-dev        - Run integration tests with pre-built images (fast)"
	@echo "  make test-service    - Service tier (T2): services from source + flag plane, no images (SERVICE_TESTS=<files> to narrow)"
	@echo "  make test-contract   - Contract tier: telemetry/infra static + flagd contract (~1 min)"
	@echo "  make test-contract-flagd - flagd contract only (one real flagd, ~1 min)"
	@echo "  make test TEST=name  - Run specific test by name"
	@echo ""
	@echo "Protos:"
	@echo "  make protos          - Generate Python protobuf files"
	@echo "  make protos-all      - Generate all protobuf files (Python, Go)"
	@echo ""
	@echo "Specs:"
	@echo "  make specs           - Regenerate docs/specs/generated/ from the code"
	@echo "  make specs-check     - Fail if docs/specs/generated/ is stale (CI)"
	@echo "  make specs-counts    - Print the flag, RPC and metric totals (not committed)"
	@echo "  make spec-refs-check - Fail on a broken path:line / path:Symbol ref in docs/specs (CI)"
	@echo "  make spec-refs       - Rewrite the undocumented-attribute baseline (growth needs a label)"
	@echo ""
	@echo "Voice catalog (#2107):"
	@echo "  make voice           - Render changed lines of services/audio/voice/lines.yaml (Piper, downloads models once)"
	@echo "  make voice-check     - Fail if the catalog, voice_manifest.json and the generated files disagree (CI, no TTS)"
	@echo "  make voice-sample    - Re-render the sample lines and compare duration and loudness"
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
	@echo "  Envoy, dashboard and connect-proxy are in the 'dashboard' profile; for the URLs below run:"
	@echo "    docker compose -f docker-compose.yml -f docker-compose.ci.yml --profile dashboard up -d"
	@echo "  Dashboard:  http://localhost/"
	@echo "  Jaeger:     http://localhost/jaeger/"
	@echo "  Metrics:    http://localhost/victoria-metrics/vmui/"
	@echo "  Grafana:    http://localhost/grafana/"

# M8 agent shadow-game dry run (#999). Brings up a RUNNABLE experiment cohort
# loop out-of-the-box: latest images (not the stale release IMAGE_TAG=0.7.0 from
# .env), mock controllers + full observability (ci flag dir), the agent & the
# dashboard profiles and the agent; the enable step below seeds one experiment
# (the experiment_seed flag, #1907) and turns the loop on.
# The flag service loads the CI baseline (services/flagd/ci) and is flagd's
# only source, so every agent write reaches the running flagd (epic #1407).
#
# Gating chain (all must be satisfied for the loop to ACT):
#   1. flagd `experiments_enabled` flag = on  -> the only experiment gate (the
#      env gate was removed in #1907). Fail-closed `off` by default and when flagd
#      cannot answer; MANUAL opt-in via the enable step below.
#   2. agent `enabled` flag = on       -> master kill-switch, fail-closed `off`
#      by default. Without it the loop self-aborts:
#      `experiment.torn_down ... reason="kill-switch: agent disabled"`.
#      (The enable step below flips BOTH 1 and 2 in one command.)
#   3. code_improvement.* promotion gates -> remain default (promotion stays off)
# Nothing writes a flag file: the operator runs the enable step, which sets
# in-memory overlays on the flag service (epic #1407).
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
	@echo "  Flags:         flag service (baseline services/flagd/ci, read-only); change: ./scripts/flags.sh"
	@echo ""
	@echo "  Experiment loop gating:"
	@echo "    [!] flagd experiments_enabled flag is the only gate (live since #1044)."
	@echo "        Fail-closed off by default and while flagd cannot answer."
	@echo "    [ ] seeded experiment after enable: experiment_seed=windows_frantic (windows = frantic music pacing, objective=balanced, N=8/arm)"
	@echo "    [ ] ENABLE experiments  -> ONE step (sets the seed, then flips enabled + experiments_enabled on):"
	@echo "        ./scripts/agent-dryrun-enable.sh on   # flag service: experiment_seed -> windows_frantic, enabled + experiments_enabled -> on"
	@echo "        # gate flips take effect LIVE (self-gated per-tick since #1044) -> NO agent restart"
	@echo "        # in-game decision backend: ./scripts/agent-backend.sh (decision.backend, live)"
	@echo "        # retro on Claude: flag inference.backend=claude + ANTHROPIC_API_KEY; proposer also AGENT_EXPERIMENT_PROPOSER_ENABLED=true + flag experiment_proposer_enabled on,"
	@echo "        #   then recreate the agent (env read at process start): ... up -d agent"
	@echo "        # restore defaults afterwards: ./scripts/agent-dryrun-enable.sh off"
	@echo "    [ ] code_improvement.* promotion gates stay OFF (no real-default promotion)"
	@echo ""
	@echo "  Observability:"
	@echo "    Dashboard:  http://localhost/"
	@echo "    Jaeger:     http://localhost/jaeger/    (agent decision-audit spans)"
	@echo "    Metrics:    http://localhost/victoria-metrics/vmui/"
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
	@for t in scripts/tests/test-*.sh; do echo "$$t"; "./$$t" >/dev/null || exit 1; done

.PHONY: format
format:
	uv run ruff format .

.PHONY: format-check
format-check:
	uv run ruff format --check .

.PHONY: check
check: lint format-check
	@echo "✓ All checks passed"

# Local PR gates (scripts/ci/local-gates.sh): lint + spec lint always, then the
# unit/contract/build gates selected from the files changed vs origin/main.
# SERVICE=menu,agent picks gates explicitly; BASE=<ref> changes the diff base.
.PHONY: pr-gates
pr-gates:
	@SERVICE="$(SERVICE)" BASE="$(or $(BASE),origin/main)" scripts/ci/local-gates.sh

# ============================================================================
# Generated spec tables (#1280)
# ============================================================================
# docs/specs/generated/*.md come from the code (metric catalog, RPC tables, flag
# sets, ci/ flag diffs) plus the hand-written sidecars in docs/specs/notes/.
# After a rebase that conflicts in a generated file: take either side, run
# `make specs`, commit. CI runs `make specs-check` (Telemetry Contract job).
# Totals (flags, RPCs, metrics) are never committed, so parallel PRs that each
# add one do not conflict (#2152): `make specs-counts` prints them, and CI
# writes them to the Telemetry Contract job summary.
SPECS_GEN = uv run --no-project --with pyyaml python scripts/gen_specs.py

.PHONY: specs
specs:
	@$(SPECS_GEN)

.PHONY: specs-check
specs-check:
	@$(SPECS_GEN) --check

.PHONY: specs-counts
specs-counts:
	@$(SPECS_GEN) --counts

# Voice catalog (#2107, UC-031 BR-007, C-050): generated voice files are
# committed (compose mounts the assets from the checkout). `make voice` after
# editing services/audio/voice/lines.yaml, commit; CI runs `make voice-check`.
VOICE_GEN = scripts/tools/gen-voice.sh

.PHONY: voice voice-check voice-sample
voice:
	@$(VOICE_GEN)

voice-check:
	@$(VOICE_GEN) --check

voice-sample:
	@$(VOICE_GEN) --sample

# Spec references (#1280): every `path:line` / `path:Symbol` in docs/specs/*.md
# must name a full repo path that exists (line in range, symbol present); Go
# Span*/Attr* values must appear in telemetry.md. `make spec-refs` rewrites the
# ATTR baseline (growth needs the spec-baseline-growth label).
# `scripts/spec_refs.py --suggest` prints path:Symbol for every path:line ref.
SPEC_REFS = python3 scripts/spec_refs.py

.PHONY: spec-refs
spec-refs:
	@$(SPEC_REFS) --update-baseline

.PHONY: spec-refs-check
spec-refs-check:
	@$(SPEC_REFS) --check $(SPEC_REFS_ARGS)

# ============================================================================
# Protobuf Generation
# ============================================================================

.PHONY: protos
protos:
	@echo "Generating Python protobuf files..."
	bash proto/generate_proto.sh

.PHONY: protos-go
protos-go:
	@echo "Generating Go protobuf files..."
	cd proto && buf generate --template buf.gen.go.yaml

.PHONY: protos-agent
protos-agent:
	@# Committed grpc-go stubs for the agent (#778); recipe and pins live in the
	@# script so CI's drift check (ci-validate-agent-protos, #1801) runs the same.
	bash scripts/ci/gen-agent-protos.sh

.PHONY: protos-all
protos-all: protos protos-go protos-agent
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

# Service tier (T2, #1422 S1 / #1524): real services from source at HEAD
# (game coordinator, controller manager, menu as subprocesses), a fresh flag
# plane per stack (go-built flag service + one flagd container) and a fake
# audio service. Needs uv, go and docker; no image build. See tests/service/README.md.
SERVICE_TEST_ENV := UV_PROJECT_ENVIRONMENT=.venv-service
# SERVICE_TESTS: files or dirs to run (default the whole tier); `make pr-gates`
# passes only the test files a diff touched. TEST stays a pytest -k keyword.
SERVICE_TESTS ?= tests/service
.PHONY: test-service
test-service:
	$(SERVICE_TEST_ENV) uv run --package joustmania-service-tests \
		pytest $(SERVICE_TESTS) -v --tb=short -p no:cacheprovider $(PYTEST_EXTRA) $(if $(TEST),-k "$(TEST)")

# Contract tier (#1357, #1422): telemetry producer/consumer static scan, infra
# config checks, and the flagd contract (one real flagd, no game stack).
# promtool, envoy and flagd run via docker when available (CI: required).
# openfeature versions pinned to uv.lock (bump together with it).
CONTRACT_DEPS = --with pytest --with pyyaml --with pytest-rerunfailures --with 'openfeature-sdk==0.10.0' --with 'openfeature-provider-flagd==0.6.1'
.PHONY: test-contract
test-contract:
	uv run --no-project $(CONTRACT_DEPS) pytest tests/contract -v -p no:cacheprovider

# Only the flagd contract (tests/contract/test_flagd_resolution.py, ~1 min), plus
# the #2184 RPC retry test and the #1945 pinned fractional buckets (need grpc +
# the provider from CONTRACT_DEPS).
.PHONY: test-contract-flagd
test-contract-flagd:
	uv run --no-project $(CONTRACT_DEPS) pytest tests/contract/test_flagd_resolution.py tests/contract/test_flagd_rpc_retry.py tests/contract/test_fractional_bucketing.py -v --tb=short -p no:cacheprovider

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

# Validate the committed agent Go stubs (services/agent/gen) match proto/ (#1801)
.PHONY: ci-validate-agent-protos
ci-validate-agent-protos:
	bash scripts/ci/validate-agent-protos.sh

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
