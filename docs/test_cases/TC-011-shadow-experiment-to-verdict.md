# Test Case: Shadow Experiment Runs to a Verdict

## Overview

**ID:** TC-011  
**Goal:** The agent runs a declared experiment as paired shadow games on pattern-driven mock controllers, scores each game, reaches a verdict and tears the experiment down, without any inference backend.  
**Priority:** High  
**Status:** Automated  
**Use cases:** [UC-043](../use_cases/UC-043.md) main (steps 1 to 8), A3, A7; [UC-046](../use_cases/UC-046.md) main; [UC-003](../use_cases/UC-003.md) A2; [UC-041](../use_cases/UC-041.md) main; [UC-040](../use_cases/UC-040.md) A1

## Roles

- Agent (declares, spawns, scores and concludes the experiment)
- Game coordinator (runs the shadow games)

## Preconditions

- The service-tier `AGENT` composition (see Automation): the agent and the one game coordinator of the stack, which the experiment has to itself. The former full-stack module `test_experiment_cohort_loop.py` and its dedicated-coordinator overlay were removed (#1422 S13); this module covers steps 1 to 8.
- `inference.backend` at its `stub` default (the env is `stub` too), `decision.backend` = `rules`, `enabled` and `experiments_enabled` on; verdict minimums lowered to 1 pair and a short experiment tick through `agent` overlays on the flag service served before the agent starts; the experiment seed is the `experiment_seed` flag's `windows_frantic` variant, rewritten by the harness (`Composition.flag_variant_values`) to `target_n` 2 and served before `enabled`.

## Flow

| Step | Name                    | Description                                                                                      | Test Data                                   | Use Case                            |
|------|-------------------------|--------------------------------------------------------------------------------------------------|---------------------------------------------|-------------------------------------|
| 1    | Start the agent         | The agent comes up on the inference-absent path and decides with rules                          | `backend=stub`, `decision_backend=rules`    | [UC-040](../use_cases/UC-040.md)    |
| 2    | Declare the experiment  | The agent writes the experiment variant and logs `experiment.declared`                           | `experiment_seed` variant                   | [UC-043](../use_cases/UC-043.md)    |
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
- The reserved mock controllers are removed; the harness stops the agent and the stack's other services.

## Automation

**Tier:** T2 service (`tests/service/`, `make test-service`): menu, game coordinator and controller manager (mock controllers) from source at HEAD, the agent built at HEAD (`AGENT` composition, with the `experiment_seed` variant rewrite and overlay (#1907) and `AGENT_EXPERIMENT_DIR` in the stack's temp dir), one flagd and flag service, fake audio, the harness `OtlpSink`. The agent has no state API, so its `slog` lines are the observable, as in the full-stack module.

Per module (#1422 S9): the units below prove each module's rules, the T2 module proves they work together.

- `tests/service/test_tc011_shadow_experiment.py`, one module-scoped stack and one live loop (about 45 s), the tests in order: `test_agent_starts_inference_absent_and_decides_with_rules` (step 1), `test_experiment_is_declared` (step 2), `test_first_game_is_a_shadow_game_on_reserved_controllers` (steps 3 and 4: own game id, registered with the flag service for the experimental arm, players are reserved controllers), `test_each_game_is_scored_for_its_arm` (steps 5 and 6), `test_verdict_is_inconclusive_and_the_experiment_is_torn_down` (steps 7 and 8; Validations 1 and 3; the postconditions: journal, shadow games deregistered, reserved controllers removed, arm value deleted), `test_no_real_game_ran_with_the_experiment` (Validation 2). One synthetic pair has no paired SD, so the verdict is always `inconclusive` and the experiment `discarded`; the `promote` path runs in the module below.
- Go unit tests of the agent (`services/agent/experiment/`, `UC: UC-043 main` / `A3` / `A7`, `UC-046 ...`): declare, spawn order and the CRN seed, scoring, the paired verdict and its give-up conditions, teardown, journal rebuild. They do not need the coordinator or the controller manager.
- The former full-stack `test_experiment_cohort_loop.py` (e2e-agent shard) and its dedicated-coordinator overlay were removed with their contract rows (#1422 S13); this service-tier test replaces them.
- `tests/service/test_tc011_promotion.py` (#1404), its own stack with a fake GitHub (`Composition(fake_github=True)`: the promoter's env gate with `GITHUB_API_URL` on an in-process stub, never github.com) and `code_improvement.mode` = `issue` (baseline). The experiment scores `endurance` and is conclusive from 2 pairs (`verdict_min_pairs` = 2, archetypes `all`, one pair each); the test, writing `eliminate_player` events as the agent's writer would, ends each control game as soon as it runs and each experimental game after 8 s, so every pair favours the experimental arm and the verdict is `promote` after four games. In order: `test_paired_games_reach_a_promote_verdict` (steps 1 to 7; flagd serves the arm value to the experiment's experimental shadow games while it runs), `test_promote_verdict_opens_one_issue` (UC-043 steps 6 and 7: exactly one issue on the stub with the agent's token and title `Flag experiment: windows`, no PR, `code_improvement.promoted` gate `issue` outcome `applied` with the stub URL, the journal's `promotion` event `applied` with that URL as reference, `experiment.torn_down` status `done`), `test_each_pair_shares_one_seed` (Validation 1: per pair index both arms share the seed; the two pairs have different seeds), `test_teardown_removes_targeting_and_spawns_nothing` (Validation 3 and the postconditions: no spawn in a 10 s window, no shadow game or reserved controller left, the flag service view of `windows` holds no experiment id and flagd serves the experimental arm the same `windows` as a real game).
- Preemption by a real game (UC-043 A2, UC-046 A8) with the agent running: `tests/service/test_tc013_real_game_preempts_shadow.py`, see [TC-013](TC-013-shadow-game-alongside-real-game.md).
