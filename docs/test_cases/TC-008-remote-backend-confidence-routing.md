# Test Case: Switch to a Remote Backend and Route by Confidence

## Overview

**ID:** TC-008  
**Goal:** An operator switches the decision backend from rules to a remote model during a Free-for-All session, and the agent acts on a confident answer, holds a low-confidence one, and falls back to rules on a broken one.  
**Priority:** Critical  
**Status:** Automated  
**Use cases:** [UC-044](../use_cases/UC-044.md) main; [UC-040](../use_cases/UC-040.md) main, A2, A5; [UC-020](../use_cases/UC-020.md) A3

## Roles

- Operator (switches the backend)
- Agent (decides)
- Remote model (the fake OpenAI-compatible server, scripted per game)

## Preconditions

- The integration stack, the agent built at HEAD and the fake OpenAI server are up through `docker-compose.decide-e2e.yml` (the `decide_stack` fixture of `tests/integration/test_decide_backend_e2e.py`); the agent has a dummy `DECISION_OPENAI_API_KEY`.
- Agent `enabled` on, `interventions_allowed` = `full`; mock controllers `MOCK0001` … `MOCK0004` connected.

## Flow

| Step | Name                        | Description                                                                                         | Test Data                                    | Use Case                            |
|------|-----------------------------|-----------------------------------------------------------------------------------------------------|----------------------------------------------|-------------------------------------|
| 1    | Switch the backend          | Operator sets `decision.backend` to `openai` with the fake's model and base URL; flagd serves it    | `openai`, model `fake-decider`                  | [UC-044](../use_cases/UC-044.md)    |
| 2    | Confident answer            | A `JoustFFA` game starts; the fake answers `adjust_global_difficulty` at high confidence            | confidence 0.95                              | [UC-040](../use_cases/UC-040.md)    |
| 3    | Verify act                  | `agent.decide` has `decide.outcome=act`, `decide.backend=openai`, a `chat {model}` child span; the coordinator publishes an unblocked `agent_intervention` | - | [UC-020](../use_cases/UC-020.md) |
| 4    | Low-confidence answer       | In the next game the fake answers the same intervention at low confidence                           | confidence 0.3                               | [UC-040](../use_cases/UC-040.md)    |
| 5    | Verify held                 | `decide.outcome=held`; no `agent.decision`, nothing written, no `agent_intervention`                | -                                            | -                                   |
| 6    | Broken answer               | In the next game the fake answers text that is not valid JSON                                       | `garbage`                                    | [UC-040](../use_cases/UC-040.md)    |
| 7    | Verify rules fallback       | `decide.outcome=fallback`, `decide.fallback_reason=invalid_answer`, and the rules engine decides     | -                                            | -                                   |
| 8    | Switch back to rules        | Operator sets `decision.backend` to `rules`; the next `agent.decide` span shows `decide.backend=rules` | `rules`                                   | [UC-044](../use_cases/UC-044.md)    |

## Validation

1. **Every decision is attributed**: each `agent.decide` span of the journey carries `decide.backend.requested` equal to the flag value of its game.
2. **Only the confident answer took effect**: exactly one unblocked `agent_intervention` across the three games, in the game of step 2.

## Postconditions

- `agent.json` and `interventions.json` are restored byte for byte by the `decide_flags` fixture; the fake's script is reset.
- Three ended games; the agent and fake containers are removed by the module fixture.

## Automation

- `tests/integration/test_decide_backend_e2e.py`: `test_openai_high_confidence_acts` (steps 1 to 3), `test_openai_low_confidence_held` (steps 4 and 5), `test_openai_garbage_falls_back_to_rules` (steps 6 and 7), `test_rules_backend_default_unchanged` (step 8). Each case runs in its own game, as the journey does.
- Gaps (planned): Jev and Claude backends (only `openai` is faked) and missing key (UC-044 A1): #1404; timeout fallback (UC-040 A2 `timeout`) and backend switch while a call is in flight (UC-044 A2, UC-040 A7 `backend_changed`): Go unit tests only (`services/agent/decision/decide_hook_test.go`, #1395), end to end #1404. The `scripts/agent-backend.sh` helper of UC-044 step 1 is tested by `scripts/tests/test-agent-backend.sh`.
