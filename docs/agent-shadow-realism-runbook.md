# Shadow realism and substrate validity runbook (#1017)

Every experiment verdict ([UC-043](use_cases/UC-043.md)) is computed on **shadow games**: mock
controllers driven by synthetic archetypes. Whether a verdict means anything for real players
depends on whether shadow play resembles real play and whether shadow fitness moves the way real
fitness moves. This runbook covers two procedures:

1. **Realism characterization** (UC-043 A22): `scripts/agent-shadow-realism.sh`, read-only,
   compares real and shadow games over a time window.
2. **Correlation study** (#1017 ask 1): a known-good and a known-bad change, each run in shadow and
   in real games, to check that the sign and rough size of the fitness change agree.

Together they are the evidence for the **validity bar** (UC-043 A23, FR-203). Until an objective
meets the bar it is human-review-only: its verdicts may open an issue or PR but never change a real
default autonomously. Enforcement in code is #2541. Autonomous promotion is off on every stack today
(`AGENT_AUTONOMOUS_ENABLED` is unset).

## 1. Realism characterization

### What it reads

Read-only: the script sends only GET requests, to the VictoriaMetrics query API
(`/victoria-metrics/api/v1/query`, `query_range`) and, unless you pass `--no-jaeger`, Jaeger's query
API v3 (`/jaeger/api/v3/traces`). It writes no flag, starts no game and runs no `docker` command.
You can run it while games are running. Real and shadow games are told apart by the
coordinator's `game_kind` label (`primary` = real, `shadow`).

### Run it on the stage Pi

The Pi's checkout is pinned to a release tag, so the script is on the Pi only once a release
includes it. Until then, run it from a laptop through an SSH tunnel. Both ways read the same data:

```bash
# from a laptop (any checkout that has the script)
ssh -f -N -L 18080:localhost:80 joustmania@192.168.1.9
scripts/agent-shadow-realism.sh --since 7d --mode FFA \
  --vm-url http://localhost:18080/victoria-metrics --jaeger-url http://localhost:18080/jaeger/api

# on the Pi, once a release ships it
ssh joustmania@192.168.1.9
cd ~/JoustMania && scripts/agent-shadow-realism.sh --since 7d --mode FFA
```

Useful options: `--start EPOCH --end EPOCH` for an exact session, `--mode FFA` (compare one mode only;
shadow games are JoustFFA, whose `mode` label is `FFA`), `--save ds.json` then
`--dataset ds.json --min-n 5` to report again with other thresholds, `--json` for machines.
VictoriaMetrics keeps about a month of data. Jaeger keeps much less, so old shadow games show no
Jaeger cross-check.

### What it measures

| Metric | Level | Definition |
|---|---|---|
| `duration` | game | final `game_duration_seconds`. The game starts at the gauge's first sample minus its first value, because the series can be exported for hours after the game ends |
| `death_rate` | kind | the kind's eliminations divided by its game minutes. In FFA every death is an elimination; respawn modes die more often than they eliminate, so compare those with `--mode`. The `game_player_deaths_total` counter is not used: on the stage Pi it resets inside a series (two writers on one series identity, restarts that keep the container id), so `increase()` read 7322 deaths for about 90 games and max-minus-before undercounts (#2543). Do not trust coordinator counters until #2543 is fixed |
| `elimination_pace` | game | eliminated players (`game_player_elimination_order` series) per minute |
| `near_death_rate` | game | `game_near_death_events_total` increase for the game's serials from 5 s before to 5 s after game end (the coordinator adds each player's count once, at game end) per player-minute. No value (counted under `skipped`) for a game that shares a serial with another game ending from 7 s before to 5 s after it (any game, also one that the kind, mode or window filter drops), and none when the counter reset inside the window (`resets()` > 0, #2543: `increase()` across such a reset read up to 1640 near-deaths for one game on the Pi). On the Pi it matched the Jaeger count on all 55 games that had both |
| `near_death_rate_jaeger` | game | the same from the `player_analytics` span events (`near_death_count`, one per player). It cross-checks the counter, which has no `game_id` label |
| `activity` | game | mean over the game's seconds of the field mean of `game_player_accel_magnitude / game_effective_death_threshold` over the alive players (at least 2) ([decide-contract §14.1](specs/decide-contract.md#141-characteristics)) |
| `change` | game | mean over 15 s windows (every 5 s) of the swing (max minus min) of the three 5 s sub-window means of field activity |
| `closeness` | game | mean over 5 s samples of the spread (max minus min) of per-player activity over the last 10 s, among the alive players |
| `survival` | game | mean over players of the fraction of the game they stayed alive (1 for a survivor). This is a game-level form that §14.1 does not define |
| `skill_gap` | game | max minus min of the final `game_player_skill_level`: the input of the balanced fitness |
| `intensity` | player, compared per game | mean `game_player_accel_magnitude` over the game (g); a game's value is the mean over its players |
| `movement_variance` | player, compared per game | final `game_player_movement_variance`; a game's value is the mean over its players |

**Verdict per metric** (every count is games; players of one game are not independent samples):
`insufficient` when either kind has fewer than `--min-n` games (default 8, the cohort's
`verdict_min_n`), or while a KS above `--ks-max` would not yet be significant at alpha 0.05: the
critical value 1.358 x sqrt((n+m)/(n x m)) must be at most `--ks-max` (0.3). That takes about 27
shadow games against 89 real ones, or 41 per kind when the counts are equal. With fewer games,
"divergent" would mostly be noise: with 8 games per kind and identical distributions it came out
divergent two times in three. The gate holds the false "divergent" from the KS branch at about
5 %. The ratio-of-medians branch has no such control: for wide distributions (lognormal sigma 1),
identical kinds still come out divergent about 15 % of the time at those counts (4-5 % at
sigma 0.6 or less). That errs on the safe side for the validity bar, but read a ratio-only
"divergent" (KS within `--ks-max`) as a hint. Otherwise `similar` when the two-sample KS
statistic is at most `--ks-max` **and** the shadow / real ratio of medians lies within
[1/`--ratio-max`, `--ratio-max`] ([0.67, 1.5]). Otherwise `divergent`. `insufficient` never counts
as similar.
**Objective roll-up:** endurance and accelerate read `duration`, balanced reads `skill_gap`
(`services/agent/decision/fitness.go`). An objective is `divergent` when any input is, `similar`
only when all are, `insufficient` otherwise.

### Caveats

- Real sessions mix test games (one or two players, a few seconds) with real play. Pick the window
  with `--start/--end` around a real session, or read the quartiles, not just the median.
- The near-death counter is keyed by serial. A game that shares a serial with another game ending
  at about the same time gets no value, rather than a mixed one.
- A naturally ended game keeps exporting its per-game series until the next game starts
  (telemetry.md). The tool places games by their first sample, so this does not matter here, but it
  does for any query that uses the last sample.
- The per-player gauges are exported about once a second, so `activity`, `change` and `closeness`
  approximate the agent's own values. Compare them between kinds only, never with the agent's numbers.
- `similar` realism is necessary but not sufficient: shadow can look like real and still respond
  differently to a flag change. Only the correlation study (section 2) tests that.

### First result (stage Pi, 2026-11-01, window 30 days)

89 real games (FFA 88, Fight Club 1; mostly the 2- and 4-player FFA games of 2026-11-01) and
5 shadow games (one experiment, `exp_3bd084a608f1`, 2026-10-28). **Every verdict is
`insufficient`**: 5 shadow games are below the floor of 8 and far below the about 27 the KS test
needs here. That includes the per-player rows, which now count games. Nothing can be called
similar or divergent yet. The medians only hint at where to look:

- `duration`: real median 16 s vs shadow 40 s, and two shadow games ran into the 300 s limit.
- `elimination_pace`: 5.6/min vs 1.6/min. `near_death_rate`: 12.5 vs 1.5 per player-minute.
- `movement_variance`: real 0.12 g² vs shadow 0.007 g². The mock drivers may move much more
  smoothly than people.
- `intensity` (1.34 g vs 1.26 g) and `activity` (0.57 vs 0.50) are close by their medians.

The next data point is more shadow games: at least 27 FFA shadow games in the window (for example
an experiment of 14 or more per arm). No objective can be judged against the bar today.

## 2. Correlation study (#1017 ask 1, needs live play)

Goal: for one objective, check that shadow and real fitness change in the **same direction**
(and roughly by the same amount) when a flag changes. Start with **endurance** (duration-based
fitness, the objective with a real signal) on the flag `sensitivity` (an experiment catalog key
that JoustFFA shadow games read, `0` strictest to `4` most lenient, default `2`).

| Arm | Value | Expected effect on endurance fitness |
|---|---|---|
| baseline | `sensitivity` 2 (medium) | reference |
| known-good | `sensitivity` 4 (ultra_fast, most lenient) | longer games, fitness up |
| known-bad | `sensitivity` 0 (ultra_slow, strictest) | shorter games, fitness down |

**Shadow side.** Run one experiment per change against the baseline, `objective: endurance`,
`target_n` of at least 8. The `experiment_seed` flag (UC-043 trigger) accepts only its declared
variants (`off`, `windows_frantic`; [flags.md](specs/flags.md)), so a `sensitivity` seed variant has
to be added to the flag file first, in a reviewed PR. The value is a flag-file change, not an
ad-hoc write. Then turn the variant on the way `scripts/agent-dryrun-enable.sh` turns on
`windows_frantic`. Read the per-arm mean fitness from the experiment's journal or its
`experiment.progress` span.

**Real side.** Run real games at each value: at least 8 games per value, the same player count
(4) and mode (FFA), and the same players across all three values in one session. Set the value
between blocks of games as the host (pad or admin mode: `sensitivity` is a persisted admin value),
never mid-game. Interleave the blocks (baseline, good, bad, baseline, ...) so warm-up and fatigue
do not line up with one value. Endurance fitness of a real game is `min(1, duration /
fitness.endurance.min_session_seconds)`. Take `duration` per game from
`scripts/agent-shadow-realism.sh --save` over each block's `--start/--end`.

**Compare.** For each pair (baseline vs good, baseline vs bad, good vs bad), compare the sign of
the difference in mean fitness: shadow vs real. Three of three agreeing is the first evidence for
the substrate. Any disagreement means the objective stays human-review-only. Record the blocks,
the means and the signs in a comment on #1017. Run the realism characterization over the same
windows too. The formal bar (UC-043 A23) needs K >= 8 values of the flag, at least 75 % sign
agreement over the value pairs and a Spearman rank correlation of at least 0.6 (lower 95 % bound
above 0). This three-value run is the smoke test before that study.

**Safety.** The real-side values are ordinary host settings. The shadow side runs only with the
agent's experiment gates on (`enabled`, `experiments_enabled`). Leave
`AGENT_AUTONOMOUS_ENABLED` unset, so a promote verdict never changes a real default (UC-043 A4).
