# Test Case: Host Restricts the Agent from the Controller

## Overview

**ID:** TC-006  
**Goal:** During a Free-for-All game the host steps the agent's permission down to `none` from a controller, and from then on the agent makes no remote call and nothing it decides takes effect in the game.  
**Priority:** Critical  
**Status:** Draft  
**Use cases:** [UC-012](../use_cases/UC-012.md) main, A6; [UC-013](../use_cases/UC-013.md) main; [UC-040](../use_cases/UC-040.md) A14, A22; [UC-045](../use_cases/UC-045.md) A2

## Roles

- Host (uses the admin combo on one controller)
- Agent (decides interventions for the running game)

## Preconditions

- The integration stack and the agent built at HEAD are up (the `decide_stack` pattern of `tests/integration/test_decide_backend_e2e.py`), agent `enabled` and `interventions_enabled` on, `interventions_allowed` = `standard`, `decision.backend` = `openai` against the fake OpenAI server (`tests/integration/fake_openai`) scripted to answer `adjust_global_difficulty` at high confidence.
- Mock controllers `MOCK0001` (host) … `MOCK0004` connected; a `JoustFFA` game runs with `MOCK0002` … `MOCK0004`.

## Flow

| Step | Name                      | Description                                                                                              | Test Data                         | Use Case                            |
|------|---------------------------|----------------------------------------------------------------------------------------------------------|-----------------------------------|-------------------------------------|
| 1    | Enter admin mode          | Host presses Cross, Circle, Square and Triangle together on the controller outside the game               | `MOCK0001`                        | [UC-012](../use_cases/UC-012.md)    |
| 2    | Select agent control      | Host presses Move until the purple `agent_control` option is selected                                    | 3 Move presses                    | [UC-013](../use_cases/UC-013.md)    |
| 3    | Restrict twice            | Host presses Start twice; the system steps `interventions_allowed` down and gives colour and voice feedback | `standard` → `ambient` → `none` | [UC-013](../use_cases/UC-013.md)    |
| 4    | Verify the flag           | flagd serves `none` for `interventions_allowed`; the host's controller pulsed red with the "off" line     | -                                 | -                                   |
| 5    | Verify no remote call     | The agent's next cycles build and send no request to the fake server and record no `agent.decide` span    | 10 s window                       | [UC-040](../use_cases/UC-040.md)    |
| 6    | Write an intervention by hand | Operator writes `eliminate_player` for `MOCK0002` into `interventions.json`; the coordinator blocks it | `eliminate_player`, `MOCK0002`    | [UC-045](../use_cases/UC-045.md)    |
| 7    | Verify blocked            | `agent_intervention` with `blocked=true`, reason `not_allowed`; `MOCK0002` stays alive                    | -                                 | [UC-040](../use_cases/UC-040.md)    |

## Validation

1. **Nothing reached the game**: no unblocked `agent_intervention` event for this game after step 4.
2. **Ladder persisted**: `agent.json` holds `none` as the `interventions_allowed` default variant.

## Postconditions

- `interventions_allowed` is `none` in the active `agent.json`; the test fixture restores the flag files (safety gates first).
- One running game that the suite fixture ends; mock controllers stay connected.

## Automation

- **Partly automated**: `tests/integration/test_intervention_flow.py::test_permission_layer_blocks_disallowed_intervention` covers steps 6 and 7 with `interventions_allowed` set to `ambient` in the flag file (not from the controller).
- Gaps: the controller ladder (steps 1 to 4, UC-013; clamped at both ends since #1373) is unit-tested only (`services/menu/tests/test_admin_handler.py`), with no end-to-end test; the "no remote call" check of step 5 (UC-040 A14) is covered by Go unit tests only (`services/agent/decision/decide_no_allowed_test.go`, #1395).
