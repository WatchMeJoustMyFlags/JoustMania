# Test Case: Shadow Experiment Runs to a Verdict

## Overview

**ID:** TC-011  
**Goal:** The agent runs a declared experiment as paired shadow games on pattern-driven mock controllers, scores each game, reaches a verdict and tears the experiment down, without any inference backend.  
**Priority:** High  
**Status:** Draft  
**Use cases:** [UC-043](../use_cases/UC-043.md) main, A3, A7; [UC-046](../use_cases/UC-046.md) main; [UC-003](../use_cases/UC-003.md) A2; [UC-041](../use_cases/UC-041.md) main; [UC-040](../use_cases/UC-040.md) A1

## Roles

- Agent (declares, spawns, scores and concludes the experiment)
- Game coordinator (runs the shadow games)

## Preconditions

- The integration stack with the dry-run and dedicated-coordinator overlays (`docker-compose.dry-run.yml`, `docker-compose.agent-coordinator.yml`) and the agent under `--profile agent`, as set up by `tests/integration/test_experiment_cohort_loop.py`.
- `AGENT_INFERENCE_BACKEND=stub`, `decision.backend` = `rules`, `enabled` and `experiments_enabled` on; verdict minimums lowered to 1 pair and a short experiment tick through `agent` overlays on the flag service; an env-seeded experiment.

## Flow

| Step | Name                    | Description                                                                                      | Test Data                                   | Use Case                            |
|------|-------------------------|--------------------------------------------------------------------------------------------------|---------------------------------------------|-------------------------------------|
| 1    | Start the agent         | The agent comes up on the inference-absent path and decides with rules                          | `backend=stub`, `decision_backend=rules`    | [UC-040](../use_cases/UC-040.md)    |
| 2    | Declare the experiment  | The agent writes the experiment variant and logs `experiment.declared`                           | env-seeded experiment                       | [UC-043](../use_cases/UC-043.md)    |
| 3    | Spawn paired games      | The agent reserves mock controllers per archetype and spawns an experimental and a control game with the same seed | archetype `quick_cascade` (`experiment.mock_archetypes`) | [UC-046](../use_cases/UC-046.md) |
| 4    | Run as shadow games     | The coordinator runs both as shadow games with their own game ids; the reserved controllers stay out of the lobby | -                         | [UC-003](../use_cases/UC-003.md)    |
| 5    | Observe and score       | The agent observes each game and scores its fitness at game end                                  | -                                           | [UC-041](../use_cases/UC-041.md)    |
| 6    | Verify samples          | Each concluded game logs a fitness value for its arm                                             | -                                           | -                                   |
| 7    | Reach a verdict         | After the minimum pairs the agent computes the paired effect and concludes (with synthetic games: inconclusive) | -                              | [UC-043](../use_cases/UC-043.md)    |
| 8    | Verify teardown         | The agent logs the terminal verdict and tears the experiment down                                 | -                                           | -                                   |

## Validation

1. **Paired by seed**: every concluded experimental game has a control game with the same seed.
2. **Real game untouched**: no real game ran with the experiment variant (see [TC-010](TC-010-ffa-game-end-retro-and-proposal.md) Validation 2).
3. **Terminated**: after step 8 the agent spawns no further game for the experiment.

## Postconditions

- The experiment is concluded and journaled on the `agent-experiments` volume; its arm values are deleted from the flag service (UC-053 A3 step 3).
- The reserved mock controllers are removed; the module fixture removes the agent and the dedicated coordinator.

## Automation

- **Partly automated**: `tests/integration/test_experiment_cohort_loop.py`: `test_inference_absent_backend_is_stub`, `test_inference_absent_degrades_to_rules` (step 1), `test_experiment_is_declared` (step 2), `test_cohort_loop_spawns_paired_games_that_conclude_with_fitness` (steps 3 to 6), `test_cohort_loop_runs_to_verdict_and_terminates` (steps 7 and 8).
- Not asserted: pairing by seed (Validation 1), no further spawns after teardown (Validation 3), the inconclusive verdict of step 7 (the test accepts `torn_down`, `promoted` or `promote_failed`), and the deletion of the arm values (Postconditions).
- Gaps (planned, #1404): those assertions, promotion on a `promote` verdict (UC-043 steps 6 and 7), and preemption by a real game (UC-043 A2, see [TC-013](TC-013-shadow-game-alongside-real-game.md)).
