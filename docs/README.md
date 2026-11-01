# JoustMania documentation: start here

JoustMania uses **spec-driven development** with the AI Unified Process (AIUP): the documents in sections 1
to 4 are the source of truth, and code and tests follow from them. Change the spec first; see
[CONTRIBUTING § Spec-driven development](CONTRIBUTING.md#spec-driven-development-aiup).

## 1. Why: vision

| Document | What it answers |
|---|---|
| [vision.md](vision.md) | What JoustMania is, for whom, the party-game vision and the talk |
| [NORTHSTAR.md](NORTHSTAR.md) | Where the self-improving agent is heading, authority tiers, hard constraints (authoritative) |
| [OWNERSHIP_MODEL.md](OWNERSHIP_MODEL.md) | Who owns which setting: human admin vs agent (authoritative contract) |

## 2. What: requirements and domain

| Document | What it answers |
|---|---|
| [requirements.md](requirements.md) | Every functional requirement (FR), measurable quality (NFR) and constraint (C), with status |
| [entity_model.md](entity_model.md) | Domain entities: controller, game session, player, flag, decision, intervention, experiment, rollout |
| [glossary.md](glossary.md) | Shared vocabulary: game kinds, decision outcomes, confidence, gates, stages, shadow, experiment, kill switch, and the words to avoid |

## 3. How it behaves: use cases

[use_cases.puml](use_cases.puml) is the overview diagram (render it with PlantUML). One file per use case:

| Area | Use cases |
|---|---|
| Pairing and controllers | [UC-001](use_cases/UC-001.md) pair over USB, [UC-002](use_cases/UC-002.md) connect and join, [UC-003](use_cases/UC-003.md) mock controllers, [UC-004](use_cases/UC-004.md) identify a controller |
| Lobby and admin | [UC-010](use_cases/UC-010.md) ready up and start, [UC-011](use_cases/UC-011.md) select mode, [UC-012](use_cases/UC-012.md) admin settings, [UC-013](use_cases/UC-013.md) agent kill switch on the controller, [UC-014](use_cases/UC-014.md) force start |
| Playing | [UC-020](use_cases/UC-020.md) FFA, [UC-021](use_cases/UC-021.md) teams, [UC-022](use_cases/UC-022.md) hidden roles, [UC-023](use_cases/UC-023.md) zombies, [UC-024](use_cases/UC-024.md) swapper, [UC-025](use_cases/UC-025.md) 1v1, [UC-026](use_cases/UC-026.md) non-stop, [UC-027](use_cases/UC-027.md) force end, [UC-030](use_cases/UC-030.md) death detection, [UC-031](use_cases/UC-031.md) audio |
| Observability | [UC-035](use_cases/UC-035.md) observe the system |
| Agent | [UC-040](use_cases/UC-040.md) decide an intervention, [UC-041](use_cases/UC-041.md) observe a game, [UC-042](use_cases/UC-042.md) retro and proposals, [UC-043](use_cases/UC-043.md) shadow experiments and promotion, [UC-044](use_cases/UC-044.md) switch backend live, [UC-045](use_cases/UC-045.md) kill switch, [UC-046](use_cases/UC-046.md) repeatable pattern-driven shadow games (#1258) |
| Infrastructure | [UC-050](use_cases/UC-050.md) progressive rollout, [UC-051](use_cases/UC-051.md) automatic rollback, [UC-052](use_cases/UC-052.md) manual canary (all three parked since 2026-10-19) |
| Operations | [UC-060](use_cases/UC-060.md) operate the stage Pi (planned, #2027), [UC-061](use_cases/UC-061.md) reach the stage Pi over its own hotspot (#2099) |
| Talk | [UC-070](use_cases/UC-070.md) present the demo |

## 4. Exact contracts: technical specs

| Document | What it answers |
|---|---|
| [specs/services.md](specs/services.md) | Services, ports, every gRPC RPC, HTTP endpoints, dependencies |
| [specs/flags.md](specs/flags.md) | Every flag: type, variants, default, readers, writers, reload behaviour |
| [specs/telemetry.md](specs/telemetry.md) | Span names, attributes, metrics, game events, log lines others depend on |
| [specs/decide-contract.md](specs/decide-contract.md) | The decision backend contract: questions, validation, confidence, outcomes, fallbacks |
| [specs/generated/](specs/generated/) | Tables generated from the code by `make specs` (Python metric catalog, gRPC RPC tables, flag sets, `ci/` flag diffs; totals are not committed, `make specs-counts` prints them, #2152); never hand-edited, hand-written notes live in [specs/notes/](specs/notes/) |
| [specs-inventory.md](specs-inventory.md) | Every older doc's status and the **discrepancies found** between docs and code |

## 5. How to: guides and runbooks

| Document | Use it to |
|---|---|
| [CONTRIBUTING.md](CONTRIBUTING.md) | Set up, follow the spec-first workflow, pass CI |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Build, run, test and debug; compose and image workflow |
| [hardware-setup-guide.md](hardware-setup-guide.md) (includes the 25-controller setup) | Set up a Pi 5 with many controllers |
| [UC-010](use_cases/UC-010.md), [UC-012](use_cases/UC-012.md) | Lobby and admin button maps (BR-005 in each) |
| [CONTROLLER_CONNECTIVITY.md](CONTROLLER_CONNECTIVITY.md) | Troubleshoot Bluetooth (stale) |
| [AUDIO.md](AUDIO.md) | Audio hardware and ALSA setup |
| [observability-quickstart.md](observability-quickstart.md) | Performance tooling (stale metric names) |
| [CANARY_RUNBOOK.md](CANARY_RUNBOOK.md) | Manual backend canary |
| [architecture/flag-service.md](architecture/flag-service.md) | Flag service: one gRPC sync source for flagd partitioned by flagSetId, `PUT /{flagSetId}/{flagName}` writes, mutable-flag registry, persisted admin settings, migration plan (epic #1407) |
| [architecture/stack.md](architecture/stack.md) | AIUP stack file: layers mapped to our repo skills (`/implement`, `/pytest-test`, `/go-test`, `/integration-test`, `/coverage-check`), markers, commands, mapping of the aiup-core 3.x commands (#2079) |
| [agent-act-runbook.md](agent-act-runbook.md) | Take the agent from inert to acting |
| [agent-group-ab-runbook.md](agent-group-ab-runbook.md) | Measure whether the LLM invents group shields: A/B script, pre-registration, jev re-calibration |
| [agent-shadow-realism-runbook.md](agent-shadow-realism-runbook.md) | Compare shadow games with real games (read-only) and run the shadow-to-real correlation study behind the validity bar (#1017) |
| [agent-dry-run-runbook.md](agent-dry-run-runbook.md) | Run the experiment loop as a dry run |
| [agent-trace-viz.md](agent-trace-viz.md) | Show agent traces on stage |
| [talk/two-standards-runbook.md](talk/two-standards-runbook.md) | Run the "Two Standards Walk Into an Agent" talk (with `scripts/talk-preflight.sh`) |
| [talk/stage-test-checklist.md](talk/stage-test-checklist.md) | Hardware test checklist for the stage Pi on the real power bank (#1465) |

Service READMEs (`services/*/README.md`) hold code-level detail; where they disagree with the specs, the
specs win and the [inventory](specs-inventory.md#3-service-tool-and-test-docs) records the difference.

## 6. Why it is like this: research and design history

Kept for the reasoning; they do not describe the current system.

| Document | Topic |
|---|---|
| [design/agent-experiment-cohorts.md](design/agent-experiment-cohorts.md) | Experiment cohorts design |
| [research/722-intervention-surface.md](research/722-intervention-surface.md) | Intervention vocabulary |
| [research/774-shadow-games.md](research/774-shadow-games.md) | Shadow games |
| [research/844-post-game-retro.md](research/844-post-game-retro.md) | Post-game retro |
| [research/m7-4-testing-strategy.md](research/m7-4-testing-strategy.md) | Testing propose / validate / promote |

### Archive

Retired notes with no inbound references, moved out of `research/`; kept for history only.

| Document | Topic |
|---|---|
| [archive/739-prompt-capture.md](archive/739-prompt-capture.md) | In-game prompt capture spike (retired by #1234) |
| [archive/spike-survival-calibration-1125.md](archive/spike-survival-calibration-1125.md) | Fitness calibration (offline evidence) |
