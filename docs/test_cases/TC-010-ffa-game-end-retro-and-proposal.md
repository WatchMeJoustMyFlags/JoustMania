# Test Case: FFA Game End Triggers Retro, Summary and an Experiment Proposal

## Overview

**ID:** TC-010  
**Goal:** When a real Free-for-All game ends, the agent runs its game-end chain in order (retro, game summary, proposer) and the proposal becomes an experiment-arm value on the flag service (UC-053 A3), while real players keep the shipped values.  
**Priority:** High  
**Status:** Automated  
**Use cases:** [UC-020](../use_cases/UC-020.md) main; [UC-041](../use_cases/UC-041.md) main; [UC-042](../use_cases/UC-042.md) main, A2; [UC-043](../use_cases/UC-043.md) main

## Roles

- Players (mock controllers driven by the operator)
- Agent (reviews the game and proposes)
- Inference backend (Claude for the model path, or the stub for Validation 3)

## Preconditions

- The integration stack and the agent built at HEAD are up; `enabled` and `experiments_enabled` on; the agent's first game end is always admitted by the proposer cadence floor (`code_improvement.min_interval_seconds` is a read-only baseline flag, so a test cannot set it); `llm.eligible_game_kinds` = `real_only` (the shipped default, value `real`).
- The retro and proposer reach a model only with the `inference.backend=claude` flag (the service tier writes the overlay at stack start; the flag wins over the env, FR-151, #1810) and an Anthropic API key in the agent's environment (value not part of this document); the proposer additionally needs its gate open: `AGENT_EXPERIMENT_PROPOSER_ENABLED=true` AND the `experiment_proposer_enabled` flag on (FR-152 d, #1810). The service tier also sets a candidate list naming no game flag, so the thesis proposer cannot declare a second experiment next to the one this journey asserts on; `AGENT_INFERENCE_BASE_URL` points the backend at a Messages API-compatible server (a fake Claude in the service tier, #1422 S10). The expected model output: a retro with one suggestion and a proposal that changes `sensitivity` for shadow games.
- Mock controllers `MOCK0001` … `MOCK0004` connected; `current_game` is `JoustFFA`.

## Flow

| Step | Name                    | Description                                                                                          | Test Data                              | Use Case                            |
|------|-------------------------|------------------------------------------------------------------------------------------------------|----------------------------------------|-------------------------------------|
| 1    | Play FFA to a winner    | Operator starts the game and eliminates three players                                                | winner `MOCK0004`                      | [UC-020](../use_cases/UC-020.md)    |
| 2    | Detect the game end     | The agent sees `game_active` go 1 → 0 (or the game root span, UC-041 A4) for the game id and starts the game-end chain                   | game id `G1` (captured)                | [UC-041](../use_cases/UC-041.md)    |
| 3    | Retro                   | The agent asks the backend for a session assessment and records an `agent.llm.retro` span for `G1`    | -                                      | [UC-042](../use_cases/UC-042.md)    |
| 4    | Game summary            | The agent adds the game to its rolling window of the last 20 games                                   | -                                      | [UC-042](../use_cases/UC-042.md)    |
| 5    | Propose                 | The proposer submits a proposal; the experiment gate accepts it                                      | `sensitivity` → `1` (`slow`) for shadow games | [UC-042](../use_cases/UC-042.md)   |
| 6    | Verify the experiment   | `GET /game/<flag>` shows a value for the proposal's experiment id (`exp_<hex>`) and arm `experimental` only | -                                      | [UC-043](../use_cases/UC-043.md)    |
| 7    | Verify real games unchanged | flagd resolves `sensitivity` to the shipped value for a real game context                        | `game_kind` = `real`                   | -                                   |

## Validation

1. **Chain order**: the span start times for `G1` are ordered retro, then summary, then `code_improvement.proposed`.
2. **Shadow-scoped only**: no real-game evaluation context resolves the experiment variant.
3. **Stub variant** (A2): with `inference.backend` at its `stub` default the retro span is capture-only and the proposer proposes nothing; the flag service holds no experiment value.

## Postconditions

- The flag service holds the experiment value until the experiment is torn down ([TC-011](TC-011-shadow-experiment-to-verdict.md)); the test fixture deletes its overlays.
- One ended game.

## Automation

- **Automated**: `tests/service/test_tc010_game_end_retro_and_proposal.py::test_tc010_game_end_runs_retro_summary_and_proposer` (**Tier:** T2 service, `AGENT_CLAUDE` composition, #1422 S10) plays an FFA menu game to a winner (`MOCK0003` of four) against the agent built at HEAD with `AGENT_INFERENCE_BACKEND=claude`, whose `AGENT_INFERENCE_BASE_URL` is the scriptable fake Claude (`tests/service/harness/fakes/claude.py`, Anthropic Messages API, 401 without the expected key). One `uc` marker per flow (`tc="TC-010"`). It asserts:
  - Steps 2 to 4: one `agent.llm.retro` span for the game id with `inference.used=claude`, the parsed assessment, one suggestion and the focus; one `agent.game.summary` span (4 players, 3 eliminations); the fake received exactly one retro and one proposal call with the key and model, the retro prompt names the game id.
  - Step 5: an unblocked `code_improvement.proposed` span for `sensitivity`; the proposer prompt offered the flag.
  - Step 6: `GET /game/sensitivity` holds exactly one experiment id (`exp_<hex>`) with the `experimental` arm only, value `1`.
  - Step 7 and Validations 1, 2: the span start times are ordered retro, summary, proposed; flagd resolves `1` for the experimental arm of that experiment and the shipped `2` for a real game and for the control arm.
- Validation 3 (stub variant, UC-042 A2) stays with `tests/service/test_tc011_shadow_experiment.py::test_agent_starts_inference_absent_and_decides_with_rules` (the agent selects the stub backend); `tests/contract/test_flagd_resolution.py::test_experiment_reaches_shadow_never_real` also covers steps 6 and 7 on a real flagd.
- Gaps: no test for the gate rejection (UC-042 A4), the shadow-game capture-only retro (UC-042 A1) or a failing model call (A9, A10).
