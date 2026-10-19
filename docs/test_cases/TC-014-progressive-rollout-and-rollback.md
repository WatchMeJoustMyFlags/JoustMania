# Test Case: Progressive Rollout with Automatic Rollback

## Overview

**ID:** TC-014  
**Goal:** An operator rolls the controller backend out progressively to the `unstable` adapter, the agent expands the first stage, detects the degradation, rolls every controller back and holds off re-expansion for the cooldown; with remediation disabled it only recommends.  
**Priority:** High  
**Status:** Draft  
Parked with UC-050 / UC-051 / UC-052 (2026-10-19): not scheduled for automation until they are un-parked.  
**Use cases:** [UC-050](../use_cases/UC-050.md) main, A2; [UC-051](../use_cases/UC-051.md) main, A1; [UC-045](../use_cases/UC-045.md) A4; [UC-041](../use_cases/UC-041.md) A1

## Roles

- Operator (starts the rollout through the rollout flags)
- Agent (evaluates infrastructure fitness and writes the stage)
- Controller manager (routes controllers to the backend of their stage)

## Preconditions

- The full stack and the agent are up with **real controllers** `A` … `F` connected on the python backend. Mock controllers cannot run this journey today: with only the mock adapter loaded the controller manager skips rollout targeting, and the `unstable` adapter wraps only the python adapter (see #1390).
- `rollout_enabled` on (an `agent` overlay on the flag service); `remediation_allowed` on, `current_controller_count` = `none`, `strategy` = `off` (the shipped default, so nothing rolls out before step 1) in the `rollout` set, set through the flag service; `AGENT_ROLLOUT_DWELL_SECONDS` = `5`, `AGENT_ROLLOUT_COOLDOWN_SECONDS` = `30`.

## Flow

| Step | Name                      | Description                                                                                     | Test Data                                     | Use Case                            |
|------|---------------------------|-------------------------------------------------------------------------------------------------|-----------------------------------------------|-------------------------------------|
| 1    | Start the rollout         | Operator sets the target backend, then the progressive strategy                                 | `target_backend` = `unstable`, `strategy` = `progressive` | [UC-050](../use_cases/UC-050.md) |
| 2    | Observe the rollout       | The agent reads the rollout state from the controller manager's health span                     | -                                             | [UC-041](../use_cases/UC-041.md)    |
| 3    | Expand the first stage    | Fitness passes on stage 0; after the dwell the agent writes the first stage                     | `current_controller_count` 0 → 1              | [UC-050](../use_cases/UC-050.md)    |
| 4    | Verify routing            | The first controller by serial (`A`) is routed to `unstable`; `B` … `F` stay on python                 | -                                             | -                                   |
| 5    | Detect degradation        | The unstable adapter delivers about 8 Hz with gaps over 50 ms; the agent finds a violation      | -                                             | [UC-051](../use_cases/UC-051.md)    |
| 6    | Roll back                 | The agent writes `none`, the controller manager routes `A` back, and `agent.infrastructure.decision` records the rollback with the log `agent.rollout_rollback` | `current_controller_count` = `none` | [UC-051](../use_cases/UC-051.md) |
| 7    | Verify cooldown           | No expansion is written for 30 s although fitness passes again on stage 0                       | 30 s window                                   | -                                   |
| 8    | Recommend only            | Operator sets `remediation_allowed` off and repeats steps 3 to 5; the agent logs `agent.rollout_rollback_recommended` and writes nothing | `remediation_allowed` = `off` | [UC-045](../use_cases/UC-045.md) |

## Validation

1. **All controllers back home**: after step 6 every controller's routing decision names the default backend.
2. **Every cycle recorded**: each agent infrastructure cycle of the journey has an `agent.infrastructure.decision` span with its outcome.

## Postconditions

- The `rollout` and `agent` overlays are deleted (strategy and target last), so the baseline returns; the agent is restarted to clear its in-memory cooldown (UC-051 A2). No flag file changed.

## Automation

- **Manual** (real controllers). Automating it needs a code change first (an `unstable` wrapper over the mock adapter and rollout-routable mock serials, with its spec change), tracked with the e2e test in #1390. Today `tests/contract/test_flagd_resolution.py::test_rollout_write_shape_resolves_via_live_flagd` proves only that the rollout flag shape the agent writes resolves through a real flagd, and `lib/tests/test_shipped_safety_defaults.py::test_remediation_allowed_flag_is_resolvable_shape` pins the `remediation_allowed` gate; the rollback, cooldown and dry-run logic is unit-tested in `services/agent/decision/infra_loop_rollback_test.go`.
- Known bug affecting the dry-run variant: #1339.
