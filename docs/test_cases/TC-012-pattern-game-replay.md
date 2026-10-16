# Test Case: Replay a Pattern-Driven Game Exactly

## Overview

**ID:** TC-012  
**Goal:** An operator runs the same archetype with the same seed twice as a pattern-driven Free-for-All shadow game and confirms that both runs give identical checkpoint fingerprints, elimination order and winner.  
**Priority:** Medium  
**Status:** Automated  
**Use cases:** [UC-046](../use_cases/UC-046.md) main; [UC-003](../use_cases/UC-003.md) A2; [UC-030](../use_cases/UC-030.md) main

## Roles

- Operator (starts the two runs and compares them)

## Preconditions

- The integration stack is up (session fixture `docker_compose`); no other game runs (`shadow_policy` lets the pattern game start).
- An archetype `it_fast_cascade` with its parameters (three players, stream 30 Hz; slot 2 crosses the death threshold first, then slot 1, and slot 0 wins) as defined in `tests/integration/test_pattern_games.py`.

## Flow

| Step | Name                     | Description                                                                                         | Test Data                                  | Use Case                            |
|------|--------------------------|-----------------------------------------------------------------------------------------------------|--------------------------------------------|-------------------------------------|
| 1    | Install a pattern game   | Operator reserves one mock controller per archetype player and starts a `JoustFFA` shadow game with the archetype and seed | archetype `it_fast_cascade`, seed `1258` | [UC-046](../use_cases/UC-046.md) |
| 2    | Run as shadow game       | The coordinator runs it with its own game id; the reserved controllers stay out of the lobby        | -                                          | [UC-003](../use_cases/UC-003.md)    |
| 3    | Organic deaths           | Players die when their smoothed signal crosses the death threshold; the game ends with a winner     | -                                          | [UC-030](../use_cases/UC-030.md)    |
| 4    | Record the run           | On controller removal the system records frame count, signal and checkpoint fingerprints per slot   | -                                          | [UC-046](../use_cases/UC-046.md)    |
| 5    | Run it again             | Operator repeats steps 1 to 4 with the same archetype and seed                                      | archetype `it_fast_cascade`, seed `1258`      | [UC-046](../use_cases/UC-046.md)    |
| 6    | Compare                  | The two records have identical checkpoint fingerprints per slot, elimination order and winner slot  | -                                          | -                                   |

## Validation

1. **Identical outcome**: elimination order and winner slot of run 2 equal those of run 1.
2. **Identical input**: every slot's checkpoint fingerprints match on their common prefix.

## Postconditions

- Two ended shadow games; their reserved pattern controllers are removed.

## Automation

- `tests/integration/test_pattern_games.py::test_pattern_game_replays_identically` (tagged `UC-046` main, steps 3 to 6 and 8) implements steps 1 to 6.
- `scripts/tests/test-pattern-replay-check.sh` tests the operator's replay script (UC-046 steps 9 and 10) with `docker` stubbed.
- Gaps: the replay script is not run against a live stack in CI (#1366).
