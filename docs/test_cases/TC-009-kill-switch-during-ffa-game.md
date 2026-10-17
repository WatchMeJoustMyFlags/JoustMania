# Test Case: Kill Switch During an FFA Game

## Overview

**ID:** TC-009  
**Goal:** An operator turns the agent off while a remote decision for a running Free-for-All game is in flight, and the late answer is discarded, nothing reaches the game, and the agent resumes when turned on again.  
**Priority:** Critical  
**Status:** Automated  
**Use cases:** [UC-045](../use_cases/UC-045.md) main, A1; [UC-040](../use_cases/UC-040.md) A6; [UC-044](../use_cases/UC-044.md) main

## Roles

- Operator (flips the kill switch)
- Agent (decides)
- Remote model (the fake OpenAI-compatible server, holding its answer)

## Preconditions

- Same stack as [TC-008](TC-008-remote-backend-confidence-routing.md): agent at HEAD, fake OpenAI server, `decision.backend` = `openai`, `enabled` and `interventions_enabled` on, `interventions_allowed` = `full`.
- Mock controllers `MOCK0001` … `MOCK0004` connected.

## Flow

| Step | Name                      | Description                                                                                         | Test Data                                  | Use Case                            |
|------|---------------------------|-----------------------------------------------------------------------------------------------------|--------------------------------------------|-------------------------------------|
| 1    | Select the remote backend | Operator selects `openai`; the fake is scripted to hold a high-confidence answer                    | hold until released (max 28 s), confidence 0.95 | [UC-044](../use_cases/UC-044.md)    |
| 2    | Start FFA                 | A `JoustFFA` game starts; the agent sends its first decide call, which the fake holds               | -                                          | [UC-040](../use_cases/UC-040.md)    |
| 3    | Verify call in flight     | The fake's request log holds a request for this game id                                             | -                                          | -                                   |
| 4    | Turn the agent off        | Operator sets `enabled` off; flagd serves it while the call is held                                 | `enabled` = `off`                          | [UC-045](../use_cases/UC-045.md)    |
| 5    | Verify discarded          | The late answer lands: `decide.outcome=discarded`, `decide.discarded_reason=kill_switch`; no `act` outcome, nothing written, no `agent_intervention` | - | [UC-040](../use_cases/UC-040.md) |
| 6    | Verify the agent is quiet | The agent records an `agent.disabled` span for the game instead of a decision                       | -                                          | [UC-045](../use_cases/UC-045.md)    |
| 7    | Turn the agent on         | Operator sets `enabled` on; the next cycle records `agent.decide` spans again                       | `enabled` = `on`                           | [UC-045](../use_cases/UC-045.md)    |

## Validation

1. **Nothing reached the game**: `interventions.json` is byte-identical to its state before step 2, and the game's events hold no `agent_intervention`.
2. **Observe without acting** (A1 variant): with `enabled` on and `interventions_enabled` off, decisions are spanned but nothing is written.

## Postconditions

- `agent.json` (kill switch first) and `interventions.json` are restored byte for byte by the `decide_flags` fixture.
- One game that the suite fixture ends.

## Automation

- `tests/integration/test_decide_backend_e2e.py::test_kill_switch_discards_inflight_answer` implements steps 1 to 6 (it waits for the `agent.disabled` span for the game before releasing the held answer).
- Validation 2 (UC-045 A1) has no end-to-end test: `lib/tests/test_shipped_safety_defaults.py::test_agent_interventions_disabled_by_default` only reads the shipped flag defaults (#1424).
- Gaps (unit-tested only since #1395; #1363 is closed): step 7 (resume) and Validation 2 are not asserted end to end; aborting running experiments on the next tick (UC-045 step 5) and the capture-only retro after the switch (UC-045 step 6) are covered by Go unit tests only (#1362).
