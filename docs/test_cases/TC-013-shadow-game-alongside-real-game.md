# Test Case: Shadow Game Alongside a Real FFA Game

## Overview

**ID:** TC-013  
**Goal:** A shadow game runs on reserved mock controllers while players play a real Free-for-All game from the menu, and interventions and force-ends addressed to one game never reach the other.  
**Priority:** High  
**Status:** Automated  
**Use cases:** [UC-010](../use_cases/UC-010.md) main; [UC-003](../use_cases/UC-003.md) A2; [UC-020](../use_cases/UC-020.md) main, A3; [UC-040](../use_cases/UC-040.md) A18; [UC-027](../use_cases/UC-027.md) A3; [UC-043](../use_cases/UC-043.md) A2

## Roles

- Players (four unreserved mock controllers in the menu game)
- Operator (starts the shadow game on reserved controllers and writes interventions as the agent would)

## Preconditions

- The integration stack is up with `shadow_policy` = `allow` (the CI default in `services/flagd/ci/game.json`); `interventions_allowed` = `full` as a flag-service overlay (so `eliminate_player` and `end_game` pass the coordinator's allow-list); the `flag_service` fixture deletes it afterwards.
- Mock controllers `MOCK0001` … `MOCK0004` connected and unreserved; `current_game` is `JoustFFA`.

## Flow

| Step | Name                         | Description                                                                                     | Test Data                                  | Use Case                            |
|------|------------------------------|-------------------------------------------------------------------------------------------------|--------------------------------------------|-------------------------------------|
| 1    | Start the real game          | Players ready up; the menu starts a real FFA game                                               | game id `R1` (captured)                    | [UC-010](../use_cases/UC-010.md)    |
| 2    | Start a shadow game          | Operator adds reserved mock controllers and starts a headless shadow FFA game on them           | 4 reserved controllers, game id `S1`       | [UC-003](../use_cases/UC-003.md)    |
| 3    | Verify lobby isolation       | The menu's controller list holds only `MOCK0001` … `MOCK0004`; events of `S1` never appear on the stream of `R1` | -                    | -                                   |
| 4    | Scoped intervention          | Operator appends an `eliminate_player` event for one shadow player to the event list of `S1`, and writes a `player_sensitivity_factor` scoped to `S1` and another shadow player | `eliminate_player`, `player_sensitivity_factor` 1.5, game `S1` | [UC-020](../use_cases/UC-020.md)    |
| 5    | Verify only the shadow game  | `S1` publishes one unblocked `agent_intervention` per write; the player dies with exactly one `player_death`, also when a later sync serves the same event list again; only the targeted player's factor changes. `R1` has no `agent_intervention`, no dead player and no changed factor. An `eliminate_player` event in the event list of `R1` then reaches `R1` | - | - |
| 6    | Addressed end_game           | Operator appends an `end_game` event to the event list of `S1`; the coordinator force-ends `S1` with reason `agent_intervention` | `end_game`, game `S1` | [UC-040](../use_cases/UC-040.md) |
| 7    | Verify the real game runs on | `R1` has no `game_force_ended`; `S1` has one                                                     | -                                          | [UC-027](../use_cases/UC-027.md)    |
| 8    | Real game preempts shadow    | With `shadow_policy` = `block` (a stack overlay on the flag service), starting a real game force-ends a live shadow game, which yields no fitness sample | `shadow_policy` = `block`    | [UC-043](../use_cases/UC-043.md)    |

## Validation

1. **No cross-game effect**: every `agent_intervention` and `game_force_ended` event carries the game id it was addressed to, and each game's roster (`GetGameState`) holds only its own controllers.
2. **Lobby restored**: when `R1` ends, the menu restores the lobby colours of `MOCK0001` … `MOCK0004` only.

## Postconditions

- Game-scoped values died with their games (UC-053 BR-003); no flag file changed; the reserved shadow controllers are removed by `headless_cleanup`; the second real game started in step 8 is ended by `ensure_game_stopped`.

## Automation

- **Automated**: `tests/integration/test_tc013_shadow_alongside_real.py::test_tc013_shadow_game_alongside_real_game` drives steps 1 to 8 and both validations in one game pair (`R1`, `S1`), with one `uc` marker per use-case flow (`tc="TC-013"`) and a `# Step n` comment per row. The flag writes go through the flag service's HTTP API like the agent's writer (the agent does not run); the `shadow_policy` overlay is deleted by the `flag_service` fixture. Per step it asserts:
  - Steps 1 to 3: both games RUNNING with different ids; no reserved serial on the button stream the menu reads, and its latest roster is exactly the four lobby controllers; `GetGameState` rosters are the lobby set for `R1` and the reserved set for `S1`; no `S1` event on `R1`'s stream.
  - Steps 4 and 5: the scoped `eliminate_player` gives one unblocked `agent_intervention` and exactly one `player_death` on `S1`, and a later sync of the same event list adds none (the test then moves the targeted factor to 0.7 and waits until it applies, so the coordinator provably re-read the list before the absence window); the gameId + serial scoped `player_sensitivity_factor` sets 1.5 on the targeted shadow player only (another shadow player stays at 1.0); `R1` gets no `agent_intervention`, and all its players stay alive at factor 1.0. An `eliminate_player` appended to the event list of `R1` then kills a player of `R1` (UC-020 A3).
  - Steps 6 and 7: the `end_game` event in the list of `S1` force-ends `S1` once with reason `agent_intervention`, publishes an unblocked `agent_intervention` `end_game` on `S1`, and declares no `game_winner`; `R1` stays RUNNING with no `game_force_ended` and no `end_game` event.
  - Validation 2: `R1` ends; the lobby controllers show orange at 30 % and no reserved controller does.
  - Step 8: with `shadow_policy` = `block` served, a shadow game `S2` starts while no real game runs; the next menu game `R2` preempts it (`game_force_ended` reason `preempted_by_real_game`, no `game_winner`, any `game_ended` has outcome `force_ended`); a further shadow start is rejected with "Shadow games blocked while a real game is running" while `R2` runs on.
- **Automated**: `tests/service/test_tc013_end_game_one_shadow.py::test_tc013_end_game_ends_only_its_shadow_game` (T2 service, `LOBBY` composition, #1404) runs steps 6 and 7 with a SECOND shadow game: a real menu game `R1` and two shadow games `S1`, `S2` on their own reserved controllers; the `end_game` event in the list of `S1` force-ends `S1` once (reason `agent_intervention`, unblocked `agent_intervention` `end_game`, no `game_winner`), while `S2` and `R1` stay RUNNING with no `game_force_ended`, no `end_game` event and every player alive (UC-040 A18, UC-027 A3).
- **Automated**: `tests/service/test_tc013_real_game_preempts_shadow.py::test_real_game_preempts_shadow_game_without_a_sample` (T2 service, `AGENT` composition, #1404) runs step 8 with the agent: an env-seeded experiment on the archetypes `all` (the first, `calm_long`, has no player die on its own) and `shadow_policy` = `block` as a stack overlay. While its first shadow game `S1` (experimental arm) runs, the menu game `R1` starts: `S1` gets one `game_force_ended` with reason `preempted_by_real_game` and no `game_winner` (UC-043 A2, UC-046 A8); the agent logs `experiment.game_released` for `S1` with that reason and `experiment.pattern_pair_rerun_preempted` for its pair, and gives no fitness sample (no `experiment.game_concluded`, no journal `game_concluded`, every arm count 0; this also holds for a rerun the agent started while `R1` was being admitted, which the coordinator preempts after admitting `R1`, #1404, unit: `test_servicer_sessions.py::test_shadow_admitted_during_real_preemption_is_preempted`); while `R1` runs the next start is refused (`experiment.spawn_failed` "Shadow games blocked while a real game is running", UC-043 A13) and only `R1` is live; once `R1` is force-ended the pair runs again, experimental first, in a new game.
- Not covered end to end: the agent's own side of `end_game` (writing it from its decide loop, refusing an un-addressed id) is unit-tested, and so is the preemption release: `services/agent/actions/writer_scoping_test.go`, `services/agent/experiment_loop_test.go` (`TestExperimentLoop_OnGameTerminal_ReleasesPreemptedGame`), `services/agent/gamerunner/runner_test.go` (`TestRunShadowGame_PreemptedIsAborted`). Coordinator units: `services/game_coordinator/tests/test_intervention_scoping.py`, `test_servicer_sessions.py`.
- Two headless games side by side (no menu game) run in `tests/service/test_tc017_parallel_mode_lifecycle.py` (up to four at once).
