# Test Case: Agent Intervenes in an FFA Game on the Rules Backend

## Overview

**ID:** TC-007  
**Goal:** The agent observes a running Free-for-All game, the rules backend decides an intervention after an early elimination, the coordinator puts it into effect in the game, and the operator follows the decision in the dashboard and in Jaeger.  
**Priority:** Critical  
**Status:** Automated  
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

**Tier:** T3 full stack (`tests/integration/`, shipped images under docker compose) for the closed loop through the real otel-collector, envoy and Jaeger; T2 service (`tests/service/`) for the rules decision written and applied with a harness sink in place of the collector (#1422 S8).

- **Automated (T3)**: `tests/integration/test_tc007_closed_loop_full_stack.py::test_tc007_closed_loop_through_collector_and_jaeger` starts the agent (interventions on: `interventions_allowed` = `standard`, `decision.backend` = `rules`) and envoy into the session stack, with one `uc` marker per use-case flow (`tc="TC-007"`) and a `# Step n` comment per row. It asserts:
  - Steps 1 to 4 and 6: a menu-started FFA game `G1` on four mock controllers, one early elimination, then an unblocked `agent_intervention` for `G1` on the game's stream: the coordinator's telemetry reached the agent through the collector's `otlp/agent` fan-out (UC-041 steps 1, 2, 5, 6) and the agent's decision took effect through the flag service (UC-040 step 13).
  - Steps 5 and 8 (data, no browser): the Agent tab's own Jaeger query through envoy (the query API v3, `GET /jaeger/api/v3/traces?query.serviceName=agent&query.operationName=agent.decide&query.searchDepth=5` plus the fixed window, `services/dashboard/src/jaeger.ts`, read by `tests/integration/jaeger_v3.py`; #2283) returns an `agent.decide` row for `G1` with `decide.backend` = `decide.backend.requested` = `rules`, no fallback, outcome `act`, `game.kind` `real` (validation 1), whose sibling `agent.decision` is unblocked and of the applied type (validation 2); UC-035 step 3, decision feed only.
  - Step 8 (Jaeger): `G1`'s trace holds the root span `Joust Free-for-All`, the phases (`initialization_phase`, `gameplay_phase`, `game_cycle`, `teardown_phase`) and one player span per controller (UC-035 step 5); `agent.decide` sits under the agent's `agent.signal_received`, a remote child of the coordinator's `game_cycle`, and each sibling `agent.decision` keeps its Link to the game root (UC-040 BR-013, rules-path clause and Link; the remote path is not run). Validation 1 over every agent span of the trace: `game.id` is `G1`, `game.kind` is `real`.
  - Envoy is dialled on its published port; when another listener answers host port 80 with a non-envoy 404 (seen under WSL) or `TC007_ENVOY_VIA_CONTAINER_IP=1` is set, the test uses envoy's compose-network address.
- **Automated (T2)**: `tests/service/test_tc007_rules_intervention_applied.py::test_tc007_rules_decision_applied_in_ffa_game` (`AGENT` composition, #1404) implements steps 1 to 4 and 6 with interventions on: an unblocked rules `agent.decision` for `G1`, the agent's game-scoped write of that intervention in the flag service (UC-040 step 12) and an unblocked `agent_intervention` of the same type (validation 2); validation 1 for the decision spans. `tests/service/test_tc008_remote_backend_routing.py::test_rules_backend_default_unchanged` covers steps 1 to 5 with interventions off.
- Hand-written interventions: `tests/service/test_intervention_smoke.py::test_shadow_experimental_interventions_apply` (T2, `GAME` composition, moved from `tests/integration` in #1422 S8) proves step 6 for every `shadow_experimental` action. The pacing and global-difficulty levers are unit-tested from flag value to game effect in `services/game_coordinator/tests/test_intervention_flag_to_effect.py` (#1422).
- Contract-level coverage of step 8's plumbing (static config checks, no live stack, no game): `tests/contract/test_envoy_routing.py` checks that envoy on port 80 routes `/`, `/jaeger/`, `/grafana/` and `/experiments` to their services (UC-035 step 1); `tests/contract/test_collector_best_effort.py` checks that the collector's fan-out to Jaeger, VictoriaMetrics and the agent is best effort (UC-035 A3, UC-041 step 2); `lib/tests/test_telemetry_fail_open.py` checks that a service keeps recording without blocking when the collector is down.
- Gaps: the rendered Agent tab (the feed fold is unit-tested in `services/dashboard/src/__tests__/decide.test.ts`) and its Jaeger and VictoriaMetrics sources (UC-035 step 3) stay manual; music override audible effect (step 7, #1362). Shield feedback (UC-040 A16) is unit-tested in `services/game_coordinator/tests/test_shield_feedback.py`.
