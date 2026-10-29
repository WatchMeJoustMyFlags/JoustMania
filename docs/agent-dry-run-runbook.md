# M8 Agent Dry-Run Runbook — a runnable experiment cohort loop

This runbook stands up the **M8 experiment cohort loop** (#982/#991) as a live
dry run: mock controllers, the full local observability stack, the agent
declaring + spawning shadow games for ONE seeded experiment, all spans flowing
to Jaeger. It is the operator counterpart to the design in the
[agent README](../services/agent/README.md) (see *Gating chain* there) and the
[ACT-path runbook](agent-act-runbook.md) (the live-intervention path).

The fastest path is:

```bash
make dry-run
./scripts/agent-dryrun-enable.sh on   # the loop is inert until you do this
                                      # (flag-service overlay, live — no restart)
```

`./scripts/agent-dryrun-enable.sh on` sets the experiment seed and flips **both** live gates in one step,
as in-memory `operator` overlays through the flag service (`scripts/flags.sh`): first the
`experiment_seed` flag (variant `windows_frantic`, #1907), then the master `enabled` kill-switch **and** the
`experiments_enabled` flag (the LIVE experiment gate post-#1044). The seed goes first so the first enabled
tick already sees it. The in-game
decision backend is a separate flag (`decision.backend`, default `rules`); flip
it with `./scripts/agent-backend.sh` (see [decide-contract.md 10.1](specs/decide-contract.md#101-the-text-backend-post-game-retro-and-proposer)).

Restore the fail-closed defaults afterwards with
`./scripts/agent-dryrun-enable.sh off` (clears all three overlays). A flag-service
restart or `down` also drops them; nothing is persisted.

`make dry-run` prints the same steps and the observability URLs on completion.

> **Why a manual step at all?** After #1044 the experiment config moved from env
> vars to flagd flags. The `experiments_enabled` flag is the only gate (the
> `AGENT_EXPERIMENTS_ENABLED` env var was removed in #1907; a host `.env` entry
> for it is ignored), and it is `off` in the baseline (#1077). The enable script
> is the single opt-in that turns the loop on without changing any production
> fail-closed default.
>
> **Refill tick (#1907):** the dry-run used to set a 15 s tick by env
> (`AGENT_EXPERIMENT_TICK_SECONDS=15`); that env var is gone, so the tick is the
> `experiment_tick_seconds` flag, default 30 s (the first refill after start
> still comes within 5 s). For a faster demo cadence:
> `./scripts/flags.sh set agent experiment_tick_seconds fast` (10 s).

---

## Why a plain `docker compose up` produces a DEAD run

Several defaults silently combine into a loop that never runs (the #999 + #1077
findings):

| # | Default | Symptom | Fix in the dry-run path |
|---|---------|---------|-------------------------|
| 1 | `.env` pins `IMAGE_TAG=0.7.0` (release-please) | A plain `up` runs the last RELEASE images — they predate the experiment framework, so the loop never constructs (no `Experiment cohort loop` log) | `make dry-run` pins `IMAGE_TAG=latest` (override with `IMAGE_TAG=... make dry-run`, or add `BUILD=1` to build locally) |
| 2 | Agent master kill-switch `agent.json` `enabled` = `off` (fail-closed) | Loop declares + writes targeting then self-aborts: `experiment.torn_down ... reason="kill-switch: agent disabled"` | **Manual opt-in** — `./scripts/agent-dryrun-enable.sh on` (in-memory overlay in the running stack only; the baseline stays `off`) |
| 3 | No experiment seed | The loop runs but has nothing to test | The seed is the `experiment_seed` flag (#1907; the `AGENT_EXPERIMENT_SEED_*` env vars are gone and a host `.env` entry for them is ignored): `./scripts/agent-dryrun-enable.sh on` sets its `windows_frantic` variant. (The `AGENT_EXPERIMENTS_ENABLED` env gate was removed in #1907 too.) |
| 5 | **(#1044/#1077)** flagd `experiments_enabled` flag = `off` (fail-closed) | The loop is constructed but `gated LIVE by agent.json experiments_enabled`; **zero** `experiment.*` activity | **Manual opt-in** — `./scripts/agent-dryrun-enable.sh on` flips `experiments_enabled` (and `enabled`, row 2) as in-memory overlays; the baseline stays `off` |
| 6 | **(#1225/#1234)** flagd `decision.backend` flag = `rules` | Decisions come from the rules engine; no model is called. Shadow games stay on rules even with a remote backend (`llm.eligible_game_kinds` defaults to `real`) | `./scripts/agent-backend.sh claude` (needs `ANTHROPIC_API_KEY` in the agent env); `rules` is the correct floor without a key |

Row 4 (the agent writing a different flag dir than flagd served, a #822-class
mismatch) is gone since the flag service (epic #1407): the agent writes through
the flag service's HTTP API, the flag service loads the `services/flagd/ci/`
baseline in this stack, and flagd's only source is the flag service.

---

## The gating chain

For the loop to actually run shadow games **and** (eventually) promote a winning
value to the real default, three independent gates must be satisfied. They are
intentionally separate so a dry run can spawn shadows without ever risking a
real-default change.

1. **Experiments opt-in** — flagd `experiments_enabled` flag = `on`.
   This **flagd flag is the only gate** (live since #1044; the env gate was
   removed in #1907 — the loop logs `gated LIVE by agent.json experiments_enabled`).
   When `off` (the fail-closed default, also while flagd cannot answer) there are
   no shadow spawns, no experiment-arm writes, no promotions.
   *Manual opt-in via `./scripts/agent-dryrun-enable.sh on` — never default-on.*

2. **Agent master kill-switch** — `agent.json` `enabled` flag = `on`.
   Read live from flagd each cycle. When `off` (the fail-closed default) the
   decision loop short-circuits before any work and emits a throttled
   `agent.disabled` span (`services/agent/decision/telemetry.go` `SpanDisabled`);
   the experiment loop aborts running experiments with the teardown reason
   `kill-switch: agent disabled`. A flag-service overlay reaches flagd over the
   sync stream at once — no restart needed; the agent re-reads it each cycle.
   *Flipped together with gate 1 by `./scripts/agent-dryrun-enable.sh on` —
   never default-on.*

   *(Optional) decision backend* — flagd `decision.backend` flag. Default
   `rules` (no model called). `./scripts/agent-backend.sh claude` switches real
   games to Claude when the agent has `ANTHROPIC_API_KEY`; shadow games stay on
   rules.

   *(Optional) post-game proposer* — with the flag `inference.backend=claude`
   (and `ANTHROPIC_API_KEY`) and `AGENT_EXPERIMENT_PROPOSER_ENABLED=true` (the proposer gate's ceiling, which
   also needs the `experiment_proposer_enabled` flag on, #1810) the M7-4
   proposer asks Claude for an experiment at game end, but only while gates
   1 and 2 are on and only after a **real** game ends (#1270): a shadow-only dry
   run produces **no** proposals. To let shadow game ends trigger it, set
   `llm.eligible_game_kinds` to `real_and_shadow`, which also enables remote
   decide calls and retros for shadow games. Proposals also share the
   `llm.max_requests_per_minute` budget (see `docs/specs/decide-contract.md` 10.1).

3. **`code_improvement.*` promotion gates** — separate flags in the agent
   domain (`code_improvement.mode`, `.target`). These gate the **real-default
   promotion** action only. They stay at their defaults in the dry run, so a
   concluded experiment produces a **verdict** (visible in traces) but does
   **not** set a `real` promotion overlay on the flag service. Promotion is also
   gated by environment variables that no compose file passes through, so the
   stock stack only records no-op promotions: `AGENT_CODE_IMPROVEMENT_ENABLED=true`
   for any promotion, plus `AGENT_AUTONOMOUS_ENABLED=true` for the real-default
   write (`services/agent/promote/realdefault.go`), or, for issue mode,
   `target=github` at startup with `GITHUB_TOKEN` and `GITHUB_REPOSITORY`
   (`promote/github.go`). Promotion also remains subject
   to the invariant gate (real-context resolution must be byte-unchanged) and
   the kill-switch.

A dry run typically exercises gates 1 + 2 only — you watch shadow games run and
verdicts form, with the real default untouched.

---

## What `make dry-run` brings up

```
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.ci.yml \
  -f docker-compose.dry-run.yml \
  --profile agent --profile dashboard up -d
```

- `IMAGE_TAG=latest` — current code, not the `.env` release pin.
- `docker-compose.ci.yml` — mock controllers (`backend=mock`), the `agent` /
  `dashboard` profiles gated behind `--profile`.
- `docker-compose.dry-run.yml` — the flag service's own admin state file
  (`admin-dry-run.json`) and the agent profile. The seed is not in compose any
  more (#1907): `agent-dryrun-enable.sh on` sets the `experiment_seed` variant
  `windows_frantic` (`windows` = the `frantic` pacing object, objective
  `balanced`, `N=8`/arm). The flag service loads the `services/flagd/ci/`
  baseline; no service mounts a writable flag dir.
- `--profile dashboard` — dashboard + connect-proxy + envoy for the full
  observability surface at `http://localhost/`.

Seed another flag instead: select another reviewed variant of `experiment_seed`
(live, no restart; the agent does not act on the value it acted on last again
until it has read `off`: `agent-dryrun-enable.sh off`, wait one
`experiment_tick_seconds`, then `on` re-runs the same seed in the same agent
process). `windows_frantic` is the only one today: the `invincibility_4_5`
variant was removed with `invincibility_seconds`'s catalog entry (#2366, a
shadow game never read it).

```bash
./scripts/flags.sh set agent experiment_seed <variant>
```

The flag service takes declared variants only, so a new seed is a new variant
of `experiment_seed` in both `services/flagd/agent.json` and
`services/flagd/ci/agent.json` (a reviewed change; `TestExperimentSeedVariants`
and `TestExperimentSeedFlagVariantsParse` check it). The seed flag must **exist in `services/flagd/ci/game.json`**, be in the
experiment catalog (a registry row with `experiment+arm` scope,
[flags.md §13.3](specs/flags.md#13-mutable-flag-registry); only flags a shadow
game reads are in it, UC-043 BR-009), and the value must
be in its bounds; the flag service rejects anything else with HTTP 422
(`flag_read_only` / `out_of_bounds`).

---

## Verifying it is alive

- **Logs**: `docker compose ... logs -f agent`. At startup the loop is always
  *constructed* (`Experiment cohort loop constructed … gated LIVE by agent.json
  experiments_enabled`, `env_enabled_default=true`) — that line alone does NOT
  mean it is running. Once `experiments_enabled` is flipped `on`, you see
  per-tick `experiment.declared` / `experiment.started` / `experiment.game_assigned`
  activity (not `kill-switch: agent disabled` and not the
  `experiments_enabled flag off` abort).
- **Jaeger** (`http://localhost/jaeger/`, service `agent`): experiment
  declare/spawn/conclude/verdict spans.
- **Flag service**: `scripts/flags.sh get game <seed flag>` lists the
  experiment-arm value (scope `experiment_id` + `arm=experimental`); the flag
  service renders it as a `game_kind != "real"` rung. The baseline file is
  never written.

## Failure modes

- **Experiments go idle after the first one concludes.** Only the one seeded
  experiment runs. Dynamic generation is the live flag
  `experiment_dynamic_enabled` (default off): run
  `scripts/flags.sh set agent experiment_dynamic_enabled on` (no restart), or
  select another seed variant as in "Seed another flag" above. The
  `AGENT_EXPERIMENT_*` env vars: `_DYNAMIC_MAX_CONCURRENT`, `_MAX_GAMES` and
  `_PROPOSER_ENABLED` are ceilings (#1810, C-034); the seed and the dynamic
  lists are flags (`experiment_seed`, `experiment_dynamic_*`); no env var is a
  fallback for a flag any more (#1907).
- **Verdicts read `inconclusive`.** Expected on mock controllers: the
  scripted movement is not a real effect, so arms rarely separate. Do not treat
  `inconclusive` as a failure; the loop is working when declare, start,
  game_assigned and conclude all appear.
- **Editing `services/flagd/` files changes nothing on a running stack.** The
  flag service reads the baseline once at start. Change flags with
  `scripts/flags.sh set <set> <flag> <value>`. `scripts/flags.sh restart`
  re-reads the baseline but drops every overlay (and game scope), including the
  dry-run enable overlays; re-run `./scripts/agent-dryrun-enable.sh on` afterwards.

## Tear down

```bash
./scripts/agent-dryrun-enable.sh off    # clear the enabled/experiments_enabled overlays (optional: down drops them too)
docker compose \
  -f docker-compose.yml -f docker-compose.override.yml \
  -f docker-compose.ci.yml -f docker-compose.dry-run.yml \
  --profile agent --profile dashboard down
```
