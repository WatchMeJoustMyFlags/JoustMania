# Stack

**For the agent:** read before writing code. This file is the AI Unified Process (aiup-core 3.x) record of the
technical layers: which skill builds and tests each layer. JoustMania has no AIUP layer plugin (none fits Python gRPC,
a Go agent, a TypeScript dashboard, Rust HID and docker compose); every skill below is a **repo skill** in
[.claude/skills/](../../.claude/skills/README.md), not a plugin skill (#2079, option 1). The core construction commands
(`/use-case-implement`, `/use-case-test`, `/test-case-implement`) read the Skill / Test skill columns; our skills stay
the per-layer workhorses.

**Last updated:** 2026-10-28

## Layers

| Layer       | Technology                                   | Skill        | Test skill                                   | Location |
|-------------|----------------------------------------------|--------------|----------------------------------------------|----------|
| Migration   | –                                            | –            | –                                            | – |
| Persistence | – (no database; state in memory + flagd flag files) | –     | –                                            | – |
| Client      | gRPC stubs, OpenFeature/flagd, OTLP          | `/implement` | `/pytest-test`, `/go-test`                   | `lib/`, generated `*_pb2*.py` / `services/agent` proto |
| Service     | Python 3.14 asyncio gRPC services; Go agent; Rust HID | `/implement` | `/pytest-test` (Python), `/go-test` (Go), Rust inline tests (`cargo test`) | `services/{audio,controller_manager,game_coordinator,menu,operator_pad,pairing_daemon,python_hid}`, `services/agent`, `services/flag-service`, `services/connect-proxy`, `services/rust-hid` |
| API         | gRPC (proto in `proto/`), Connect proxy for browsers | `/implement` | `/pytest-test`, `/go-test`            | `proto/`, `services/connect-proxy` |
| UI          | TypeScript dashboard (browser, via connect-proxy); headless controller UI (LED, rumble, voice) | `/implement` | dashboard tests (vitest, `describe('UC-xxx: ...')`); controller UI via `/pytest-test` | `services/dashboard` |
| E2E         | docker compose stack (`tests/integration`, `tests/service`) | – | `/integration-test`                    | `tests/` |

**Integration layer:**

| Use case offered through | Tested in | Test skill |
|--------------------------|-----------|------------|
| UI                       | Integration layer (docker compose; controller input via the mock HID / service tier) | `/integration-test` |
| API                      | Integration layer (gRPC against the compose stack) | `/integration-test` |
| Service                  | Service unit tests, per language | `/pytest-test`, `/go-test` |

A use case is tested once, in the first row its `**Layers:**` line names (none of our use cases carry that line yet,
so the first row, the integration layer, applies to cross-service flows; single-service flows are covered by the
service-level tests). `Tested` status still requires the full bar in
[.claude/skills/coverage-check](../../.claude/skills/coverage-check/SKILL.md): every main step and alternative flow
has a tagged test.

**E2E scope:** Use cases and test cases

## External systems

| System | Contract | Location |
|--------|----------|----------|
| flagd (feature flags) | `docs/specs/flags.md` | `services/flagd`, `services/flag-service` |
| OTEL collector / Jaeger / VictoriaMetrics / Grafana | `docs/specs/telemetry.md` | `services/otel-collector` and siblings |
| PS Move controllers (HID, Bluetooth) | `docs/specs/services.md` | `services/rust-hid`, `lib/psmove_hid.py` |
| LLM decision backends (Jev, Claude, OpenAI) | `docs/specs/decide-contract.md` | `services/agent` |

## Module layout

Multi-service monorepo; the topology, ports and RPCs live in [docs/specs/services.md](../specs/services.md). Python
services follow `server.py` / `servicer.py` / `metrics.py` / `tests/` / `pyproject.toml`; shared code is `lib/` (uv
workspace). Tests live next to each service; cross-service tests are in `tests/`.

## API contract

Contract-first: the `.proto` files in `proto/` are the contract and generated code follows (`make protos`). A new RPC
also needs a forwarder in `services/connect-proxy/main.go`. Generated specs: `docs/specs/generated/` (`make specs`).

## Commands

| Purpose            | Command |
|--------------------|---------|
| Build              | `make protos-all`, `docker compose build` |
| Lint               | `make lint` |
| Unit tests         | Python `cd services/<svc> && uv run --extra test pytest -q --tb=short`; Go `go vet ./... && go test -race ./...`; dashboard `npx tsc --noEmit && npx vitest run`; Rust `cargo clippy -- -D warnings && cargo test` |
| Integration tests  | `make test` (docker compose; PR CI is the evidence, see [CONTRIBUTING](../CONTRIBUTING.md)) |
| Service tests      | `make test-service` |
| PR gates           | `make pr-gates` |
| Spec lint          | `scripts/aiup/spec-lint-gate.sh` (the same script CI's Spec Gate runs) |

## Markers

| Layer | Skill | Kind | Marker to grep for |
|-------|-------|------|--------------------|
| Python services, lib | `/pytest-test` | Test | `@pytest.mark.uc("UC-040", flow="A2", br=[...])`, `nfr` markers |
| Go agent, connect-proxy | `/go-test` | Test | `// UC: UC-040 A2` and `UC-040/A2 ...` subtests |
| Dashboard | dashboard tests | Test | `describe('UC-040: ...')` |
| Rust HID | Rust inline tests | Test | `// UC: UC-040` / `// NFR:` lines |
| Shell / integration scripts | `/integration-test` | Test | `# UC: UC-040` header line in `scripts/tests/test-*.sh` |
| All   | `/implement` | Code | `// UC-XXX BR-YYY:` directly above the code that enforces the rule |

## Mapping of the AIUP 3.x core commands to our skills

| Core command (aiup-core 3.x) | Our equivalent | Decision |
|------------------------------|----------------|----------|
| `/stack` | this file, edited by hand | Skill names are repo skills; `/stack` discovers skills by `references/layer.md`, which ours do not carry. Do not rerun `/stack` blindly: it owns Layers / External systems / Markers and may rewrite them. |
| `/use-case-implement UC-xxx` | `/implement UC-xxx` | Keep `/implement`: it knows our spec set (`docs/specs/*`), flags and generated-spec rules. |
| `/use-case-test UC-xxx` | `/pytest-test`, `/go-test`, `/integration-test` | Keep ours (per-language markers and the integration tier above). |
| `/use-case-coverage UC-xxx` | `/coverage-check UC-xxx` | **Keep `/coverage-check` only**; ignore `/use-case-coverage` so two coverage commands cannot drift. Ours embeds the Tested bar, FR/UC status drift and the Known gap rule, and delegates to the repo `uc-coverage` agent. |
| `/test-case-implement`, `/test-case-coverage` | `/coverage-check TC-xxx`; TC automation per `docs/test_cases` | Not adopted yet. |
| `/spec-review` | vendored `scripts/aiup/spec-review` + the plugin skill | Same lint, same version (aiup-core v3.10.0). |
