# Group-shield A/B runbook (#2467)

Question: does an LLM decision backend come up with group shields on its own when the lever is offered and described
neutrally? We enable the capability and measure; no rule fires a group shield (UC-040 A47, BR-033, BR-036, C-034).

## Safety

- Shadow games only. The script never writes `policy.group_grant.real_games`; leave it `off`.
- The LLM budget is global: run with no real game live. The script refuses to start, and aborts between games, when
  `GET /games` of the flag service lists a game that is not a shadow game (an unknown kind counts as real).
- The stack's agent container is stopped for the run (it would decide in the one-shot game too) and restarted on exit.
- Every flag the run writes is snapshotted first and restored on exit, error, SIGINT or SIGTERM.
  Manual recovery: `scripts/agent-group-ab.sh restore --out <dir>`. The flags: `decision.prompt_profile`,
  `decision.backend`, `interventions_allowed`, `llm.eligible_game_kinds`, `experiment_dynamic_enabled`,
  `policy.group_grant.enabled`.
- Cap the cost with `llm.max_requests_per_minute=stage` before the run.

## Run

```bash
scripts/agent-group-ab.sh run --archetype statue_finale --seeds 1,2,3 --reps 2 --backend claude --out /tmp/ab-claude
scripts/agent-group-ab.sh report --out /tmp/ab-claude            # Jaeger, default http://localhost/jaeger/api
```

Games run A,B then B,A per seed (arm A `decision.prompt_profile=v1`, arm B `neutral`; group shields are on in both).
`--archetype` is any archetype of `experiment.mock_archetype`; the finale archetypes of the original plan
(`statue_finale`, `drift_finale`, `jittery_finale`) are not shipped yet, so use an existing one.

The report counts only games whose remote `agent.decide` spans carry the arm's `decide.prompt_profile`
(others are listed as excluded) and prints per arm: group grants (applied, blocked by reason), when they happened
(alive players at the group, last third of the game), single grants, and the outcomes. Metrics from the same run:
`agent_group_interventions_total{outcome,reason,backend,game_kind,shape}`.

## Pre-registration (fix before running)

- Grid: at least 3 seeds, 2 repetitions per arm, per backend (jev, claude, openai), per archetype.
- Primary measures: group grants per game by arm; share of groups decided with at most 3 alive; share in the last third.
- Success: set the threshold and n before the first run, and write them in the issue. The script reports and does not judge.
- Fairness: `shape=all_except` share, zero partial applies, zero group grants with the flag off or in a real game.
- Limits: shadow games with open-loop players show invention only, not that a group shield livens a real game.
  jev gives no rationale, so judge it by choice rates.

## Re-calibrating the jev gate under `neutral`

The wording of `neutral` moves jev's gate p_yes (one sentence moved it by 0.07, #1281). Before any stage use of
`decision.prompt_profile=neutral` with jev, run the four calibration states under both profiles and compare:

```bash
cd services/agent
JEV_API_KEY=... go test -tags live ./decide/backends/jev -run 'TestLiveGate$' -v
JEV_API_KEY=... JEV_LIVE_PROFILE=neutral go test -tags live ./decide/backends/jev -run 'TestLiveGate$' -v
```

If p_yes per state moved, adjust `decision.jev.act_confidence_threshold` (human-owned) and record the numbers in the
issue. This is an operator step: it calls the real API, so CI does not run it.
