# Test Case: Agent Intervenes in an FFA Game on the Rules Backend

## Overview

**ID:** TC-007  
**Goal:** The agent observes a running Free-for-All game, the rules backend decides an intervention after an early elimination, the coordinator puts it into effect in the game, and the operator follows the decision in the dashboard and in Jaeger.  
**Priority:** Critical  
**Status:** Draft  
**Use cases:** [UC-010](../use_cases/UC-010.md) main; [UC-041](../use_cases/UC-041.md) main; [UC-040](../use_cases/UC-040.md) A1, step 13, step 14, A16; [UC-020](../use_cases/UC-020.md) A3; [UC-030](../use_cases/UC-030.md) main; [UC-031](../use_cases/UC-031.md) A3; [UC-035](../use_cases/UC-035.md) main

## Roles

- Players (four mock controllers driven by the operator)
- Agent (observes and decides)
- Operator (watches the dashboard and Jaeger)

## Preconditions

- The integration stack and the agent built at HEAD are up; agent `enabled` and `interventions_enabled` on, `interventions_allowed` = `standard`, `decision.backend` = `rules` (the shipped default).
- Mock controllers `MOCK0001` … `MOCK0004` connected; `current_game` is `JoustFFA`.

## Flow

| Step | Name                     | Description                                                                                           | Test Data                        | Use Case                            |
|------|--------------------------|-------------------------------------------------------------------------------------------------------|----------------------------------|-------------------------------------|
| 1    | Start FFA                | Operator readies all four controllers; the game starts                                                | trigger on all four              | [UC-010](../use_cases/UC-010.md)    |
| 2    | Observe the game         | The agent receives the game's telemetry and builds one context for the game id                        | game id `G1` (captured)          | [UC-041](../use_cases/UC-041.md)    |
| 3    | Eliminate early          | Operator simulates a death on one player in the first seconds                                        | `MOCK0001`                       | [UC-030](../use_cases/UC-030.md)    |
| 4    | Rules decide             | The rules backend ranks its candidates and emits an intervention                                      | -                                | [UC-040](../use_cases/UC-040.md)    |
| 5    | Verify the decision span | Jaeger holds an `agent.decide` span for `G1` with `decide.backend=rules`, `decide.backend.requested=rules`, no fallback, beside the rules `agent.decision` | - | - |
| 6    | Coordinator applies      | The coordinator puts the intervention into effect through the game's own code and publishes `agent_intervention` (not blocked) | - | [UC-020](../use_cases/UC-020.md) |
| 7    | Hear the effect          | A tempo or volume intervention overrides the music schedule for the rest of the game                  | -                                | [UC-031](../use_cases/UC-031.md)    |
| 8    | Follow it in the dashboard | Operator opens the Agent tab and finds the decision in the feed, then the game trace in Jaeger with the agent spans under `game_cycle` | `http://localhost/` | [UC-035](../use_cases/UC-035.md) |

## Validation

1. **One game, one context**: every agent span of the journey carries game id `G1` and `game_kind` `real`.
2. **Decision took effect**: the intervention type on the unblocked `agent_intervention` equals the one on `agent.decision`.
3. **Shield feedback** (when the rules picked `grant_shield`): `player_shielded` with `kind` `full` and the dashboard tile shows the shield badge until `until_ms`.

## Postconditions

- The intervention lived in the game's scope on the flag service and was dropped when the game ended (UC-053 BR-003); the test fixture deletes its `agent` overlays. No flag file changed.
- One game that the operator or the suite fixture ends.

## Automation

- **Partly automated**: `tests/service/test_tc008_remote_backend_routing.py::test_rules_backend_default_unchanged` (T2 service, `AGENT` composition, #1600) implements steps 1 to 5 (with interventions off, so it observes the decision, not its effect); `tests/integration/test_intervention_smoke.py::test_shadow_experimental_interventions_apply` proves step 6 for hand-written interventions. The pacing and global-difficulty levers are unit-tested from flag value to game effect in `services/game_coordinator/tests/test_intervention_flag_to_effect.py` (#1422).
- Contract-level coverage of step 8's plumbing (static config checks, no live stack, no game): `tests/contract/test_envoy_routing.py` checks that envoy on port 80 routes `/`, `/jaeger/`, `/grafana/` and `/experiments` to their services (UC-035 step 1); `tests/contract/test_collector_best_effort.py` checks that the collector's fan-out to Jaeger, VictoriaMetrics and the agent is best effort (UC-035 A3, UC-041 step 2); `lib/tests/test_telemetry_fail_open.py` checks that a service keeps recording without blocking when the collector is down.
- `tests/service/test_tc007_rules_intervention_applied.py::test_tc007_rules_decision_applied_in_ffa_game` (T2 service, `AGENT` composition, #1404) implements steps 1 to 4 and 6 with interventions ON (`interventions_allowed` = `standard`, `decision.backend` = `rules`): after the early elimination it waits for an unblocked rules `agent.decision` for `G1`, the agent's game-scoped write of that intervention in the flag service, and an unblocked `agent_intervention` of the same type on the game's stream (validation 2); validation 1 for the decision spans (`game.kind` `real`).
- Gaps (planned): dashboard Agent tab and Jaeger parenting (#1404); music override audible effect (#1362). Shield feedback (UC-040 A16) is unit-tested in `services/game_coordinator/tests/test_shield_feedback.py`.
