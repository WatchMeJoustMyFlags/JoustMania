# Test Cases (AIUP test-case layer)

End-to-end journeys that chain several use cases, in the format of the AIUP `test-case` skill
(`aiup-core` plugin; template: Overview, Roles, Preconditions, Flow, Validation, Postconditions).
A test case is the document an end-to-end test automates (`/integration-test TC-XXX`); the test then
carries `@pytest.mark.uc("UC-xxx", flow=..., tc="TC-XXX")` per use-case flow it exercises and a
`# Step <n>: <name>` comment per Flow row (see [.claude/skills/README.md](../../.claude/skills/README.md)).

JoustMania additions to the template:

- **`**Use cases:**`** in the Overview lists every linked use case with the flows the journey exercises.
- **`## Automation`** names the tests that implement the journey (path::test), what they do not cover, and
  the issue that plans the rest. `Manual` means the journey needs real hardware or real model backends.

Two deliberate deviations from the skill's writing rules:

- **Verification rows may link a use case.** The skill puts `-` in the Use Case column of verification
  rows; here a verification row links the use case whose outcome it checks when that use case appears in
  no action row (e.g. UC-020 when the system, not an actor, declares the winner), so every covered flow
  has a Flow row.
- **Protocol terms are allowed.** As in the use cases (see CONTRIBUTING, "Identifiers in use cases"),
  steps name flags, game events, span names and attributes, and connect-proxy paths, because those
  identifiers are what the tests observe.

Flow ids follow the use cases as of the spec catch-up #1381 (e.g. UC-010 A6, UC-040 A22).

## How the spec lint uses them

`scripts/aiup/spec-review/scripts/spec_lint.py` (the Spec Gate) reads every `TC-*.md` here: ids must be
unique and every `[UC-xxx](../use_cases/UC-xxx.md)` link must resolve. `--trace` shows the requirement
coverage stage "Every use case in a test case": a requirement reaches it when every active use case that
lists it is linked from at least one test case. "Verified" is a separate stage: it is derived from the use
cases' status (`Tested`), which `/coverage-check` grants only when every main scenario and alternative flow
has a tagged test. A test case alone never makes a requirement Verified.

## Scope

Free-for-All is the current focus: TC-001 to TC-016 are FFA journeys or mode-independent (agent, rollout,
canary, talk); TC-018 to TC-020 cover the flag service (UC-053, epic #1407). The other game modes (UC-021 to UC-026) are covered by one backlog journey, TC-017, so they
stay traceable; their detailed journeys are backlog (#1361).

## Index

| ID | Journey | Use cases | Priority | Status | Automation |
|---|---|---|---|---|---|
| [TC-001](TC-001-ffa-game-night-hardware.md) | FFA game night on real controllers | UC-001, UC-002, UC-010, UC-020, UC-030, UC-031 | Critical | Draft | Manual (hardware); mock twin TC-002 |
| [TC-002](TC-002-ffa-mock-game-to-winner.md) | FFA game to a winner on mock controllers | UC-003, UC-002, UC-011, UC-010, UC-020, UC-030 | Critical | Automated | `test_tc002_ffa_game_to_winner_then_restart`; warning effect unit-tested only |
| [TC-003](TC-003-ffa-player-disconnects.md) | Player disconnects during an FFA game | UC-003, UC-010, UC-020, UC-030, UC-002 | High | Automated | `tests/service/test_tc003_ffa_player_disconnects.py`; menu list not read |
| [TC-004](TC-004-operator-stops-ffa-game.md) | Operator stops an FFA game | UC-010, UC-020, UC-027 | High | Automated | `tests/service/test_tc004_operator_stops_ffa_game.py`; dashboard start path (UC-010 A6) untested |
| [TC-005](TC-005-host-tunes-and-force-starts.md) | Host tunes the game in admin mode and force-starts | UC-012, UC-011, UC-014, UC-010 | High | Automated | T2: `test_tc005_admin_journey::test_host_tunes_and_force_starts`; gaps unit-tested only (#1400) |
| [TC-006](TC-006-host-restricts-agent-from-controller.md) | Host restricts the agent from the controller | UC-012, UC-013, UC-040, UC-045 | Critical | Draft | Partly: T2 `test_tc006_agent_ladder` (steps 1-4), `test_tc006_agent_blocked_by_host` (steps 5-7) |
| [TC-007](TC-007-agent-intervenes-in-ffa-game.md) | Agent intervenes in an FFA game on rules | UC-010, UC-041, UC-040, UC-020, UC-030, UC-031, UC-035 | Critical | Automated | T3: `test_tc007_closed_loop_full_stack` (collector, envoy, Jaeger); T2: `test_tc007_rules_intervention_applied`; tab rendering manual, music effect #1362 |
| [TC-008](TC-008-remote-backend-confidence-routing.md) | Switch to a remote backend and route by confidence | UC-044, UC-040, UC-020 | Critical | Automated | T2: `test_tc008_remote_backend_routing` (openai fake; missing key or model, UC-044 A1), `test_tc008_jev_backend` (Jev fake), `test_tc008_claude_backend` (Claude fake); timeout #1404 (timeout unit-tested, #1395) |
| [TC-009](TC-009-kill-switch-during-ffa-game.md) | Kill switch during an FFA game | UC-045, UC-040, UC-044 | Critical | Automated | T2: `test_tc009_kill_switch::test_kill_switch_discards_inflight_answer` (incl. resume) |
| [TC-010](TC-010-ffa-game-end-retro-and-proposal.md) | FFA game end: retro, summary, proposal | UC-020, UC-041, UC-042, UC-043 | High | Automated | `test_tc010_game_end_runs_retro_summary_and_proposer` (T2); stub variant partly |
| [TC-011](TC-011-shadow-experiment-to-verdict.md) | Shadow experiment runs to a verdict | UC-043, UC-046, UC-003, UC-041, UC-040 | High | Automated | T2: `tests/service/test_tc011_shadow_experiment.py` (inconclusive verdict), `test_tc011_promotion.py` (promote verdict, issue on a GitHub stub); preemption: `test_tc013_real_game_preempts_shadow.py` |
| [TC-012](TC-012-pattern-game-replay.md) | Replay a pattern-driven game exactly | UC-046, UC-003, UC-030 | Medium | Automated | `tests/service/test_tc012_pattern_game_replay.py`; script test in CI (#1389) |
| [TC-013](TC-013-shadow-game-alongside-real-game.md) | Shadow game alongside a real FFA game | UC-010, UC-003, UC-020, UC-040, UC-027, UC-043 | High | Automated | `test_tc013_shadow_game_alongside_real_game`; T2: `test_tc013_end_game_one_shadow.py`, `test_tc013_real_game_preempts_shadow.py` (agent side of the preemption) |
| [TC-014](TC-014-progressive-rollout-and-rollback.md) | Progressive rollout with automatic rollback | UC-050, UC-051, UC-045, UC-041 | High | Draft | Manual (real controllers); mock support + e2e #1390 |
| [TC-015](TC-015-canary-by-hand.md) | Canary a backend by hand | UC-052, UC-035 | Medium | Draft | Manual; contract tests for routes; `/canary/*` handler tests (#1388) |
| [TC-016](TC-016-talk-demo-rehearsal.md) | Talk demo rehearsal | UC-070, UC-044, UC-045, UC-040, UC-042, UC-035 | Medium | Draft | Manual; script tests in CI (#1389) |
| [TC-017](TC-017-non-ffa-modes-round-backlog.md) | One round of every non-FFA mode (backlog) | UC-021 to UC-026 | Low | Draft | Partly (`tests/service/test_tc017_parallel_mode_lifecycle.py`); backlog #1361 |
| [TC-018](TC-018-agent-intervention-through-flag-service.md) | Agent intervention through the flag service | UC-053, UC-040 | Critical | Automated | T2: `test_tc018_flag_service_eliminations::test_two_eliminations_through_the_flag_service`; failed read: unit test |
| [TC-019](TC-019-flag-service-restart.md) | Flag service restart | UC-053 | High | Automated | `test_tc019_flag_service_restart` (T2 service); epoch reset: unit test |
| [TC-020](TC-020-host-and-stage-change-flags.md) | Host and stage change flags at runtime | UC-012, UC-053, UC-044 | High | Automated | T2: `test_tc020_host_and_stage_flags::test_host_and_stage_change_flags_at_runtime` |

Status follows the skill: `Automated` = an end-to-end test implements the journey (gaps listed in the
file), `Draft` = not or only partly automated, or written for review. A service-tier test (T2,
`tests/service/`: real services from source on a fresh flag plane, audio and collector faked) counts as
end-to-end for this purpose; the TC's Automation section then starts with a `**Tier:**` line (#1422). Tests that implement a journey carry
`tc="TC-XXX"` (the T2 journeys so far, TC-010 and TC-011 included); tagging the rest is part of #1356 and #1422.
