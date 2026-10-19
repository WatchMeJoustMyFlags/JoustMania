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
| 6    | Send an intervention by hand | Operator appends `eliminate_player` for `MOCK0002` to the game's event list through the flag service; the coordinator blocks it | `POST /interventions/eliminate_player` | [UC-045](../use_cases/UC-045.md)    |
| 7    | Verify blocked            | `agent_intervention` with `blocked=true`, reason `not_allowed`; `MOCK0002` stays alive                    | -                                 | [UC-040](../use_cases/UC-040.md)    |

## Validation

1. **Nothing reached the game**: no unblocked `agent_intervention` event for this game after step 4.
2. **Ladder persisted**: the flag service holds `none` as the persisted admin value of `interventions_allowed` (UC-053 A2); `agent.json` is unchanged.

## Postconditions

- `interventions_allowed` is `none` as a persisted admin value; the test fixture deletes it and every stack overlay it set (`flag_service` fixture).
- One running game that the suite fixture ends; mock controllers stay connected.

## Automation

**Tier:** T2 service for steps 1 to 4 (`tests/service/`, `make test-service`): menu, game coordinator and controller manager (mock controllers) from source at HEAD, flag service and one flagd, fake audio (`LOBBY` composition). Steps 6 and 7 stay on the integration tier.

- Precondition difference: the service test runs the `LOBBY` composition with 4 controllers, no game and no agent (the "outside the game" host).
- `tests/service/test_tc006_agent_ladder.py::test_host_walks_the_agent_ladder_from_the_controller` covers steps 1 to 4 and Validation 2 (#1598): admin combo, three Move presses to `agent_control`, then the whole ladder with Select (`none` to `full`) and back with Start, asserting after every press the persisted admin value of `interventions_allowed`, the per-level voice on the fake audio service (`adminop_off`, `adminop_1` to `adminop_3`) and, at the end, the value flagd serves. It presses past both ends (Start at `none`, Select at `full`) and asserts the value does not wrap (#1340, clamped since #1373), and starts from `probe` to cover UC-013 A1 (jump to `none`).
- `tests/integration/test_intervention_flow.py::test_permission_layer_blocks_disallowed_intervention` covers steps 6 and 7 with `interventions_allowed` set to `ambient` as a flag-service overlay (not from the controller).
- **Status stays Draft** (partly automated). Gaps: no colour feedback of steps 3 and 4 is asserted (turquoise, orange, green, and the 800 ms red pulse, too short to read through `GetColor` without a sleep); only the voice is checked; step 5 (UC-040 A14, no remote call) needs the agent composition (S7 of #1422) and is covered by Go unit tests only (`services/agent/decision/decide_no_allowed_test.go`, #1395); the chained controller-to-blocked journey (steps 1 to 7 with `none`) does not exist yet.
