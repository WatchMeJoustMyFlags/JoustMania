# M8 Agent Dry-Run Runbook — a runnable experiment cohort loop

This runbook stands up the **M8 experiment cohort loop** (#982/#991) as a live
dry run: mock controllers, the full local observability stack, the agent
declaring + spawning shadow games for ONE seeded experiment, all spans flowing
to Jaeger. It is the operator counterpart to the design in the
[agent README](../services/agent/README.md) (see *Gating chain* there) and the
[ACT-path runbook](agent-act-runbook.md) (the live-intervention demo).

The fastest path is:

```bash
make dry-run
./scripts/agent-dryrun-enable.sh on   # the loop is inert until you do this
docker compose -f docker-compose.yml -f docker-compose.ci.yml --profile agent restart agent
```

`./scripts/agent-dryrun-enable.sh on` flips **both** live gates in the ci/ flag
dir in one step — the master `enabled` kill-switch **and** the
`experiments_enabled` flag (the LIVE experiment gate post-#1044). With an
inference backend configured, also exercise `mode=llm`:

```bash
AGENT_INFERENCE_BACKEND=openai ./scripts/agent-dryrun-enable.sh on   # also flips mode -> llm
```

Restore the fail-closed defaults afterwards with
`./scripts/agent-dryrun-enable.sh off`.

`make dry-run` prints the same steps and the observability URLs on completion.

> **Why a manual step at all?** After #1044 the experiment config moved from env
> vars to flagd flags. The `experiments_enabled` flag is now the LIVE gate and it
> **overrides** the `AGENT_EXPERIMENTS_ENABLED=true` env that
> `docker-compose.dry-run.yml` sets — so the env alone produces **zero**
> experiment activity (#1077). The enable script is the single opt-in that turns
> the loop on without changing any production fail-closed default.

---

## Why a plain `docker compose up` produces a DEAD run

Several defaults silently combine into a loop that never runs (the #999 + #1077
findings):

| # | Default | Symptom | Fix in the dry-run path |
|---|---------|---------|-------------------------|
| 1 | `.env` pins `IMAGE_TAG=0.7.0` (release-please) | A plain `up` runs the last RELEASE images — they predate the experiment framework, so the loop never constructs (no `Experiment cohort loop` log) | `make dry-run` pins `IMAGE_TAG=latest` (override with `IMAGE_TAG=... make dry-run`, or add `BUILD=1` to build locally) |
| 2 | Agent master kill-switch `agent.json` `enabled` = `off` (fail-closed) | Loop declares + writes targeting then self-aborts: `experiment.torn_down ... reason="kill-switch: agent disabled"` | **Manual opt-in** — `./scripts/agent-dryrun-enable.sh on` (flips the **ci** flag dir only; production default stays `off`) |
| 3 | The agent compose env did not declare `AGENT_EXPERIMENTS_ENABLED` / `AGENT_EXPERIMENT_SEED_*` | A host `export` never reached the container (needed a custom override) | These vars are now declared (default-off) on the `agent` service in `docker-compose.yml`; the dry-run override turns them on |
| 5 | **(#1044/#1077)** flagd `experiments_enabled` flag = `off` (fail-closed) **overrides** the `AGENT_EXPERIMENTS_ENABLED=true` env | The loop is constructed but `gated LIVE by agent.json experiments_enabled` — `env_enabled_default=true` is dead config; **zero** `experiment.*` activity | **Manual opt-in** — `./scripts/agent-dryrun-enable.sh on` flips `experiments_enabled` (and `enabled`, row 2) in the **ci** flag dir only; production default stays `off` |
| 6 | **(#1044/#1077)** flagd `mode` flag = `rules` (fail-closed) | Even with experiments on, the inference backend is never exercised (stays on the rules engine) | `AGENT_INFERENCE_BACKEND=openai ./scripts/agent-dryrun-enable.sh on` flips `mode -> llm` in the ci flag dir; without a backend `rules` is correct (stub) |

And one **silent-ineffective** trap:

| # | Default | Symptom | Fix |
|---|---------|---------|-----|
| 4 | In ci-mode, `flagd`+`menu` mount `services/flagd/ci:/etc/flagd` but the base `agent` keeps `services/flagd:/etc/flagd` | The agent's game.json targeting writes land in the BASE file while flagd serves the CI file — writes happen but never reach flagd; experiment targeting is silently ineffective (a #822-class dir mismatch) | `docker-compose.dry-run.yml` `!override`s the agent volume onto the SAME `services/flagd/ci` dir flagd + menu serve |

---

## The gating chain

For the loop to actually run shadow games **and** (eventually) promote a winning
value to the real default, three independent gates must be satisfied. They are
intentionally separate so a dry run can spawn shadows without ever risking a
real-default change.

1. **Experiments opt-in** — flagd `experiments_enabled` flag = `on`.
   Post-#1044 this **flagd flag is the LIVE gate** and **overrides** the
   `AGENT_EXPERIMENTS_ENABLED=true` env (which `docker-compose.dry-run.yml` still
   sets, but is now dead config for this purpose — the loop logs
   `gated LIVE by agent.json experiments_enabled`). When `off` (the fail-closed
   default) there are no shadow spawns, no targeting writes, no promotions.
   *Manual opt-in via `./scripts/agent-dryrun-enable.sh on` — never default-on.*

2. **Agent master kill-switch** — `agent.json` `enabled` flag = `on`.
   Read live from flagd each cycle. When `off` (the fail-closed default) the
   loop short-circuits before any work and emits a throttled
   `kill-switch: agent disabled` span. Hot-reloaded — no restart needed for the
   flag flip itself, but the agent re-reads it each cycle.
   *Flipped together with gate 1 by `./scripts/agent-dryrun-enable.sh on` —
   never default-on.*

   *(Optional) inference backend* — flagd `mode` flag = `llm`. Default `rules`
   (the stub backend, correct without a backend). When an inference backend is
   configured, `AGENT_INFERENCE_BACKEND=openai ./scripts/agent-dryrun-enable.sh on`
   also flips `mode -> llm` so the dry run exercises the LLM decision path.

3. **`code_improvement.*` promotion gates** — separate flags in the agent
   domain (`code_improvement.mode`, `.target`, `.validation_games`,
   `.fitness_improvement_threshold`, …). These gate the **real-default
   promotion** action only. They stay at their defaults in the dry run, so a
   concluded experiment produces a **verdict** (visible in traces) but does
   **not** rewrite the real game.json default. Promotion also remains subject
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
- `docker-compose.dry-run.yml` — `AGENT_EXPERIMENTS_ENABLED=true`, one seeded
  experiment (`death_grace_period_seconds = 0.75`, objective `balanced`,
  `N=8`/arm), and the **agent volume `!override`d onto `services/flagd/ci`** so
  its writes reach flagd.
- `--profile dashboard` — dashboard + connect-proxy + envoy for the full
  observability surface at `http://localhost/`.

Seed another flag instead:

```bash
AGENT_EXPERIMENT_SEED_FLAG=invincibility_seconds \
AGENT_EXPERIMENT_SEED_VALUE=4.5 \
make dry-run
```

The seed flag must **exist in `services/flagd/ci/game.json`** and the value's
JSON kind must match its variants (the Gate's type guard rejects a mistyped
seed at the targeting write).

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
- **flagd**: `services/flagd/ci/game.json` gains a reserved experiment variant +
  a `game_kind != "real"` targeting branch for the seeded flag — confirming the
  agent and flagd share the file.

## Tear down

```bash
docker compose \
  -f docker-compose.yml -f docker-compose.override.yml \
  -f docker-compose.ci.yml -f docker-compose.dry-run.yml \
  --profile agent --profile dashboard down
./scripts/agent-dryrun-enable.sh off    # restore the fail-closed defaults (enabled/experiments_enabled/mode)
```
