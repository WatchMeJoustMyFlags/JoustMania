# Test Case: Talk Demo Rehearsal

## Overview

**ID:** TC-016  
**Goal:** The presenter rehearses the talk on one running Free-for-All game: observe on rules, switch to Jev, tighten the threshold, flip to the Claude backup, pull the kill switch, roll back, and show the retro at game end, then resets the stage and passes the pre-flight check.  
**Priority:** Medium  
**Status:** Draft  
**Use cases:** [UC-070](../use_cases/UC-070.md) main, A7, A8; [UC-044](../use_cases/UC-044.md) main, A3; [UC-045](../use_cases/UC-045.md) main; [UC-040](../use_cases/UC-040.md) main, A5, A6; [UC-042](../use_cases/UC-042.md) main; [UC-035](../use_cases/UC-035.md) main

## Roles

- Presenter (runs the stage scripts and narrates)
- Agent (decides)
- Players (four controllers in the demo game)

## Preconditions

- The demo stack runs per [the talk runbook](../talk/two-standards-runbook.md) with the stage baseline (`scripts/talk-stage.sh reset`), `JEV_API_KEY` and the Claude key configured in the presenter's `.env` (values not part of this document).
- `scripts/talk-preflight.sh` passes; four controllers connected; a `JoustFFA` game runs on `decision.backend` = `rules`.

## Flow

| Step | Name                    | Description                                                                                          | Test Data                                   | Use Case                            |
|------|-------------------------|------------------------------------------------------------------------------------------------------|---------------------------------------------|-------------------------------------|
| 1    | Observe on rules        | Presenter shows the dashboard Agent tab and an `agent.decide` span with `decide.backend=rules`        | `http://localhost/`                         | [UC-035](../use_cases/UC-035.md)    |
| 2    | Switch to Jev           | Presenter switches the backend; calm play yields `noop`, a collapsing game yields `act`               | `scripts/agent-backend.sh jev`              | [UC-044](../use_cases/UC-044.md)    |
| 3    | Tighten the threshold   | Presenter sets the strict Jev threshold; answers below it are `held`                                  | `scripts/agent-backend.sh jev-threshold strict` (0.95) | [UC-040](../use_cases/UC-040.md) |
| 4    | Permissions beat        | Presenter drops the allow-list to `none` for 12 s; no new decide rows appear, then the baseline returns | `scripts/talk-stage.sh permissions none 12` | [UC-070](../use_cases/UC-070.md) |
| 5    | Flip to the backup      | Presenter selects Claude; decisions show `decide.backend=claude`                                      | `scripts/agent-backend.sh claude`           | [UC-044](../use_cases/UC-044.md)    |
| 6    | Kill switch             | Presenter turns the agent off right after a Claude call starts; the late answer is `discarded` with `kill_switch` | `scripts/agent-killswitch.sh off`, then `on` | [UC-045](../use_cases/UC-045.md) |
| 7    | Roll back               | Presenter switches back to Jev; no container restarts                                                 | `scripts/agent-backend.sh jev`              | [UC-044](../use_cases/UC-044.md)    |
| 8    | Show the retro          | The game ends; the `agent.llm.retro` span shows the session assessment and suggestions                | -                                           | [UC-042](../use_cases/UC-042.md)    |
| 9    | Reset the stage         | Presenter resets the stage baseline and runs the pre-flight check, which passes                       | `scripts/talk-stage.sh reset`, `scripts/talk-preflight.sh` | [UC-070](../use_cases/UC-070.md) |

## Validation

1. **Stage baseline restored**: after step 9 every baseline flag in `agent.json` is at its stage variant and `interventions.json` at its neutral variants.
2. **No restart**: the agent container's start time is unchanged across the journey.

## Postconditions

- Flag files at the stage baseline; one ended game.

## Automation

- **Manual** (needs the Jev and Claude backends). Script-level tests run in `make lint`: `scripts/tests/test-talk-stage.sh` (UC-070 A7, A8, stage baseline), `scripts/tests/test-agent-backend.sh` (UC-044 steps 1 and 2, A3), `scripts/tests/test-talk-preflight.sh` (step 9).
- Steps 3 and 6 on a faked backend are covered by [TC-008](TC-008-remote-backend-confidence-routing.md) and [TC-009](TC-009-kill-switch-during-ffa-game.md). Running the script tests in CI: #1366.
