# Test Case: Progressive Rollout with Automatic Rollback

## Overview

**ID:** TC-014  
**Goal:** An operator rolls the controller backend out progressively to the `unstable` adapter, the agent expands the first stage, detects the degradation, rolls every controller back and holds off re-expansion for the cooldown; with remediation disabled it only recommends.  
**Priority:** High  
**Status:** Draft  
**Use cases:** [UC-050](../use_cases/UC-050.md) main, A2; [UC-051](../use_cases/UC-051.md) main, A1; [UC-045](../use_cases/UC-045.md) A4; [UC-041](../use_cases/UC-041.md) A1

## Roles

- Operator (starts the rollout through the rollout flags)
- Agent (evaluates infrastructure fitness and writes the stage)
- Controller manager (routes controllers to the backend of their stage)

## Preconditions

- The integration stack and the agent built at HEAD are up; mock controllers `MOCK0001` … `MOCK0006` connected on the default backend.
- `rollout_enabled` on in `agent.json`; `remediation_allowed` on, `current_controller_count` = `none`, `strategy` = `immediate` in `rollout.json`; `AGENT_ROLLOUT_DWELL_SECONDS` = `5`, `AGENT_ROLLOUT_COOLDOWN_SECONDS` = `30`.

## Flow

| Step | Name                      | Description                                                                                     | Test Data                                     | Use Case                            |
|------|---------------------------|-------------------------------------------------------------------------------------------------|-----------------------------------------------|-------------------------------------|
| 1    | Start the rollout         | Operator sets the target backend, then the progressive strategy                                 | `target_backend` = `unstable`, `strategy` = `progressive` | [UC-050](../use_cases/UC-050.md) |
| 2    | Observe the rollout       | The agent reads the rollout state from the controller manager's health span                     | -                                             | [UC-041](../use_cases/UC-041.md)    |
| 3    | Expand the first stage    | Fitness passes on stage 0; after the dwell the agent writes the first stage                     | `current_controller_count` 0 → 1              | [UC-050](../use_cases/UC-050.md)    |
| 4    | Verify routing            | `MOCK0001` is routed to `unstable`; `MOCK0002` … `MOCK0006` stay on the default                 | -                                             | -                                   |
| 5    | Detect degradation        | The unstable adapter delivers about 8 Hz with gaps over 50 ms; the agent finds a violation      | -                                             | [UC-051](../use_cases/UC-051.md)    |
| 6    | Roll back                 | The agent writes `none`, the controller manager routes `MOCK0001` back, and `agent.infrastructure.decision` records the rollback with the log `agent.rollout_rollback` | `current_controller_count` = `none` | [UC-051](../use_cases/UC-051.md) |
| 7    | Verify cooldown           | No expansion is written for 30 s although fitness passes again on stage 0                       | 30 s window                                   | -                                   |
| 8    | Recommend only            | Operator sets `remediation_allowed` off and repeats steps 3 to 5; the agent logs `agent.rollout_rollback_recommended` and writes nothing | `remediation_allowed` = `off` | [UC-045](../use_cases/UC-045.md) |

## Validation

1. **All controllers back home**: after step 6 every controller's routing decision names the default backend.
2. **Every cycle recorded**: each agent infrastructure cycle of the journey has an `agent.infrastructure.decision` span with its outcome.

## Postconditions

- `rollout.json` and `agent.json` are restored to the preconditions (strategy and target last); the agent is restarted to clear its in-memory cooldown (UC-051 A2).

## Automation

- **Planned** (#1390). Today `tests/integration/test_rollout_plumbing.py` proves only that the rollout flag shape the agent writes resolves through live flagd (`test_rollout_write_shape_resolves_via_live_flagd`, `test_remediation_allowed_flag_is_resolvable_shape`); the rollback, cooldown and dry-run logic is unit-tested in `services/agent/decision/infra_loop_rollback_test.go`.
- Known bug affecting the dry-run variant: #1339.
