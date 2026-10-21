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

- The agent built at HEAD runs next to the game services (service tier `AGENT` composition, `tests/service/harness`, #1600), agent `enabled` and `interventions_enabled` on, `interventions_allowed` = `standard`, `decision.backend` = `openai` against the fake OpenAI server (`tests/integration/fake_openai`) scripted to answer `adjust_global_difficulty` at high confidence.
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

**Tier:** T2 service for steps 1 to 7 (`tests/service/`, `make test-service`): menu, game coordinator and controller manager (mock controllers) from source at HEAD, flag service and one flagd, fake audio (`LOBBY` composition for steps 1 to 4; `AGENT` composition for steps 5 to 7: plus the agent built at HEAD, the harness `OtlpSink` and the fake OpenAI server).

- Precondition difference: the service test runs the `LOBBY` composition with 4 controllers, no game and no agent (the "outside the game" host).
- `tests/service/test_tc006_agent_ladder.py::test_host_walks_the_agent_ladder_from_the_controller` covers steps 1 to 4 and Validation 2 (#1598): admin combo, three Move presses to `agent_control`, then the whole ladder with Select (`none` to `full`) and back with Start, asserting after every press the persisted admin value of `interventions_allowed`, the per-level voice on the fake audio service (`adminop_off`, `adminop_1` to `adminop_3`) and, at the end, the value flagd serves. It presses past both ends (Start at `none`, Select at `full`) and asserts the value does not wrap (#1340, clamped since #1373), and starts from `probe` to cover UC-013 A1 (jump to `none`).
- `tests/service/test_tc006_agent_blocked_by_host.py::test_agent_makes_no_remote_call_after_host_restricts_it` covers steps 5 to 7 (#1600, #1422): the host steps the agent from `standard` to `none` from the controller (admin combo, `agent_control`, Start twice; persisted admin value, flagd serves `none`), a `JoustFFA` game starts, and once the agent reports `agent_llm_gated_total{reason=no_interventions_allowed}` a 10 s window shows no request to the fake server and no `agent.decide` span for the game while that counter keeps growing; no unblocked `agent_intervention` reaches the game (Validation 1 for that window). Precondition difference: the host restricts in the lobby before the game starts and then plays in it (admin mode runs in the lobby only).
- The same test covers steps 6 and 7 (coordinator half, marked `# Step 6` / `# Step 7`): the `eliminate_player` event is appended to the running game's event list through the flag service like the agent would, the coordinator blocks it (`blocked=true`, `not_allowed`) and the victim stays alive, with `interventions_allowed` at `none` as set from the controller. The former integration-tier test `test_intervention_flow.py` is removed (#1422); the `ambient` case (`eliminate_player` blocked `not_allowed`, victim alive) is covered by the unit test `test_ambient_allow_list_blocks_eliminate_player_and_victim_stays_alive` in `services/game_coordinator/tests/test_flag_set_consistency.py` (UC-040 A22).
- **Status stays Draft** (partly automated). Gaps: no colour feedback of steps 3 and 4 is asserted (turquoise, orange, green, and the 800 ms red pulse, too short to read through `GetColor` without a sleep); only the voice is checked; the chained journey is one test for steps 1 to 4 (restrict from the controller, a helper) and 5 to 7 (blocked), but it asserts the ladder's persisted value only, not the voice or colour of steps 3 and 4 (those stay in `test_tc006_agent_ladder`).
