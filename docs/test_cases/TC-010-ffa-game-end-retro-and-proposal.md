# Test Case: FFA Game End Triggers Retro, Summary and an Experiment Proposal

## Overview

**ID:** TC-010  
**Goal:** When a real Free-for-All game ends, the agent runs its game-end chain in order (retro, game summary, proposer) and the proposal becomes a shadow-scoped experiment in `game.json`, while real players keep the shipped values.  
**Priority:** High  
**Status:** Draft  
**Use cases:** [UC-020](../use_cases/UC-020.md) main; [UC-041](../use_cases/UC-041.md) main, A3; [UC-042](../use_cases/UC-042.md) main, A2, A4; [UC-043](../use_cases/UC-043.md) main

## Roles

- Players (mock controllers driven by the operator)
- Agent (reviews the game and proposes)
- Inference backend (stub, or a fake Claude-compatible server for the model path)

## Preconditions

- The integration stack and the agent built at HEAD are up; `enabled` and `experiments_enabled` on; `code_improvement.min_interval_seconds` set to `0` so the first game end may propose; `llm.eligible_game_kinds` = `real`.
- Inference backend scripted to return a retro with one suggestion and a proposal that changes `sensitivity` for shadow games.
- Mock controllers `MOCK0001` … `MOCK0004` connected; `current_game` is `JoustFFA`.

## Flow

| Step | Name                    | Description                                                                                          | Test Data                              | Use Case                            |
|------|-------------------------|------------------------------------------------------------------------------------------------------|----------------------------------------|-------------------------------------|
| 1    | Play FFA to a winner    | Operator starts the game and eliminates three players                                                | winner `MOCK0004`                      | [UC-020](../use_cases/UC-020.md)    |
| 2    | Detect the game end     | The agent sees `game_active` go 1 → 0 for the game id and starts the game-end chain                   | game id `G1` (captured)                | [UC-041](../use_cases/UC-041.md)    |
| 3    | Retro                   | The agent asks the backend for a session assessment and records an `agent.llm.retro` span for `G1`    | -                                      | [UC-042](../use_cases/UC-042.md)    |
| 4    | Game summary            | The agent adds the game to its rolling window of the last 20 games                                   | -                                      | [UC-042](../use_cases/UC-042.md)    |
| 5    | Propose                 | The proposer submits a proposal; the experiment gate accepts it                                      | `sensitivity` → `slow` for shadow games | [UC-042](../use_cases/UC-042.md)   |
| 6    | Verify the experiment   | `game.json` holds an `agent_experiment__<id>` variant targeted at that experiment's id and arm only  | -                                      | [UC-043](../use_cases/UC-043.md)    |
| 7    | Verify real games unchanged | flagd resolves `sensitivity` to the shipped value for a real game context                        | `game_kind` = `real`                   | -                                   |

## Validation

1. **Chain order**: the span start times for `G1` are ordered retro, then summary, then `code_improvement.proposed`.
2. **Shadow-scoped only**: no real-game evaluation context resolves the experiment variant.
3. **Stub variant** (A2): with `AGENT_INFERENCE_BACKEND=stub` the retro span is capture-only and the proposer proposes nothing; `game.json` is unchanged.

## Postconditions

- `game.json` holds the experiment's targeting until the experiment is torn down ([TC-011](TC-011-shadow-experiment-to-verdict.md)); the test fixture restores `game.json` and `agent.json`.
- One ended game.

## Automation

- **Partly automated**: `tests/integration/test_experiment_cohort_loop.py::test_inference_absent_backend_is_stub` asserts the agent selects the stub backend for retro and proposer (the precondition of Validation 3); `tests/integration/test_experiment_shadow_isolation.py::test_experiment_reaches_shadow_never_real` proves steps 6 and 7 for a hand-written experiment.
- Gaps (planned, #1362 "agent game-end chain"): no test plays a game to its end and asserts the retro, summary and proposer spans in order, the gate rejection (UC-042 A4) or the shadow-game capture-only retro (UC-042 A1).
