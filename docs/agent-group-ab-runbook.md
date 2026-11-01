# Group-shield A/B runbook (#2467)

Question: does an LLM decision backend come up with group shields on its own when the lever is offered and described
neutrally? We enable the capability and measure; no rule fires a group shield (UC-040 A47, BR-033, BR-036, C-034).

## What the script guarantees (and what it does not)

- **The flags are global.** The six flags it writes (`decision.prompt_profile`, `decision.backend`,
  `interventions_allowed`, `llm.eligible_game_kinds`, `experiment_dynamic_enabled`, `policy.group_grant.enabled`)
  are registered with stack scope only; none can be limited to shadow games. While a run is up, a real game sees
  them too: the permissive `shadow_group` allow-list, the chosen backend, the neutral wording.
- **Real games are protected by the guard, not by the scope.** The script refuses to start while the flag service
  lists a game that is not a shadow game (an unknown kind counts as real), while `policy.group_grant.real_games` is
  not `off`, and when `interventions_allowed` is `none` (persisted, or a stack overlay: the kill switch) or unreadable. It checks
  again before every game, every 3 s during a game, while waiting for the first-game verification and after the last game, and aborts within one poll (container removed, flags restored, agent
  started). A real game that starts in the seconds before a poll still sees the experiment flags. Run only when no
  real game will start.
- The LLM budget is global. Cap the cost with `llm.max_requests_per_minute=stage` before the run.
- The stack's agent container is stopped for the run (it would decide in the game too) and started again on exit.
- **Verification.** After the first game the script checks that an `agent.decide` span of that game carries the
  arm's `decide.prompt_profile`. If not, it stops: the throwaway agent saw nothing (the collector sends spans to
  the `agent` network alias, which the one-off container does not have). Retry with `--use-aliases`; then the
  throwaway agent observes every game, real ones included, so keep the guard in mind. `--no-verify` skips the check.
- **Restore.** The snapshot keeps each flag's overlay with its writer and remaining TTL. On exit, error, SIGINT,
  SIGTERM or SIGHUP each flag is restored only while its overlay is still the `group-ab` one: an overlay another writer set
  during the run, or one an admin write cleared, is left as it is (and reported). A run refuses to start when
  `snapshot.tsv` exists in its `--out`, or when a touched flag already carries a `group-ab` overlay.

### Manual recovery

SIGKILL (or a lost terminal) skips the trap. Run `scripts/agent-group-ab.sh restore --out <dir>`: it restores the
flags from `<dir>/snapshot.tsv`, removes leftover `group-ab-*` containers and starts the agent. It is safe to run
twice. If the agent stays down, `docker compose start agent`; a reboot also starts it and drops the in-memory overlays.

## Run

```bash
scripts/agent-group-ab.sh run --archetype slow_cascade --seeds 1,2,3 --reps 2 --backend claude --out /tmp/ab-claude
scripts/agent-group-ab.sh report --out /tmp/ab-claude            # Jaeger, default http://localhost/jaeger/api
```

Games run A,B then B,A per seed (arm A `decision.prompt_profile=v1`, arm B `neutral`; group shields are on in both).
`--archetype` is any shipped archetype (`experiment.mock_archetypes`: `calm_long`, `slow_cascade`,
`jittery_novices`, `close_race`, `late_burst`, `quick_cascade`). The finale archetypes of the original plan
(`statue_finale`, `drift_finale`, `jittery_finale`) are not shipped; a third arm (v1 without the lever) is not
implemented either.

The report counts only games whose remote `agent.decide` spans carry the arm's `decide.prompt_profile`
(others are listed as excluded) and prints per arm: group grants (applied, blocked by reason), when they happened
(alive players at the group, last third of the game, measured against the wall clock around the game, which includes
the container's start-up of a few seconds), single grants, the outcomes, and the applied groups per seed with B minus A
(the games are seed-matched). A repeated refusal emits no span and is not counted in `agent_group_interventions_total` either; repeats are only in `agent_decisions_total{action=grant_group_shield}`, so the blocked counts of the report are first refusals.

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
