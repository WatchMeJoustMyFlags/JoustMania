# Agent Demo Runbook — setup, failure modes, recovery

The operator playbook for the **live stage demo** of the agentic control stack:
the experiment cohort loop declaring + concluding experiments, the agent making
decisions (rules engine or a remote decision backend), live interventions, the dashboards, and live flag flips
via flagd.

This runbook is the **glue** between the focused runbooks — it does not duplicate
them. Read the linked one when you need the detail:

- [`agent-dry-run-runbook.md`](agent-dry-run-runbook.md) — the experiment cohort
  loop (`make dry-run`, the enable script, the gating chain). **The core of the
  demo.**
- [`agent-act-runbook.md`](agent-act-runbook.md) — the live-intervention ACT path
  (two gates, the kill-switch panic button, blocked vs applied interventions).
- [`decide-contract.md` 10.1](specs/decide-contract.md#101-the-text-backend-post-game-retro-and-proposer) — which model the agent calls for
  what: the `decision.backend` flag in game, the Claude text backend for the
  retro and the proposer.

> **Do a full dry-rehearsal of every step in this runbook on the demo hardware
> before the talk.** Several steps (venue WiFi to the model API, API keys,
> controller pairing) are environment-specific and only fail on stage.

---

## 1. Setup / bring-up

### Prerequisites

- **Docker + Docker Compose** on the demo host. That alone runs the entire
  stack with mock controllers and the **rules** decision backend (the default
  `decision.backend=rules`) — no LLM, no key, no hardware required. This is the
  safe fallback the whole demo can run on.
- *(Optional, for a remote model)* an API key for the backend you want to show
  (`ANTHROPIC_API_KEY`, `DECISION_OPENAI_API_KEY`) and internet access from the
  demo host. See *Switch to a remote decision backend* below.
- *(Optional, real PS Move controllers)* a Bluetooth adapter; the demo runs on
  **mock controllers** by default (the `services/flagd/ci/` baseline that the
  flag service loads for `make dry-run` / `up-mock` sets controller `backend=mock`), so hardware is not on the critical path.

### Bring up the demo stack (the cohort loop)

The demo runs on the **dry-run stack** — latest images, mock controllers, full
observability, the agent + dashboard profiles, and one seeded experiment. From
the repo root:

```bash
make dry-run
```

The loop is **inert until you opt in** (both live gates are fail-closed `off` by
default). Flip them on — the gate flips take effect **live** (a flag-service
overlay reaches flagd over the sync stream; NFR-050 targets p95 200 ms; since #1044
the loop is always built and self-gates each tick on the live flags, so an
off→on flip starts it with **no agent restart**):

```bash
./scripts/agent-dryrun-enable.sh on
```

`agent-dryrun-enable.sh on` sets, as in-memory `operator` overlays through the
flag service (the baseline files are never written), two flags in one step:

- `enabled` → `on` — the master kill-switch.
- `experiments_enabled` → `on` — the LIVE experiment-loop gate (post-#1044; this
  flag **overrides** the `AGENT_EXPERIMENTS_ENABLED` env, which is dead config
  for this purpose).

`make dry-run` prints these exact steps and the observability URLs on
completion. The full gating-chain rationale and the "why a plain `up` produces a
dead run" table live in the [dry-run runbook](agent-dry-run-runbook.md).

> The seeded experiment is on the **`game.windows`** flag (music-tempo pacing /
> "frantic music pacing windows"), objective `balanced`, `N=8` per arm
> (`docker-compose.dry-run.yml`). It is a **real FFA difficulty lever** (#1090),
> so the verdict is meaningful, not cosmetic.

### Switch to a remote decision backend (optional)

In-game decisions come from the `decision.backend` flag: `rules` (default),
`claude`, `openai` or `jev`. The agent calls the vendor directly (no gateway).
Export the key **before** the agent starts (keys are read at process start), then
flip the flag live:

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or DECISION_OPENAI_API_KEY for openai
docker compose -f docker-compose.yml -f docker-compose.ci.yml --profile agent up -d agent

./scripts/agent-backend.sh claude haiku   # live flag-service overlay, no restart
./scripts/agent-backend.sh status
./scripts/agent-backend.sh rules          # back to the floor
```

- Shadow games stay on rules (`llm.eligible_game_kinds` defaults to `real`), so
  remote decisions show up in **real** (menu-started) games.
- If the backend errors, times out or has no key, the rules engine decides and
  the `agent.decide` span records `decide.fallback_reason`, so a bad network is
  **never** an agent outage.
- The post-game retro and the experiment proposer can also call Claude:
  `AGENT_INFERENCE_BACKEND=claude` (agent restart). See
  [`decide-contract.md` 10.1](specs/decide-contract.md#101-the-text-backend-post-game-retro-and-proposer).

### Observability URLs

The whole stack is proxied through envoy on port **80**:

| Tool | URL | Use on stage |
|------|-----|--------------|
| **Dashboard (SPA)** | `http://localhost/` | the demo front-of-house: **Agent** tab (decision feed) + **Experiments** tab (cohorts/verdicts) |
| **Jaeger** | `http://localhost/jaeger/` | agent decision-audit spans (service `agent`): `agent.signal_received → agent.decision → agent.action`, experiment declare/conclude/verdict spans |
| **Grafana** | `http://localhost/grafana/` | dashboards (see act 3 below) |
| **VictoriaMetrics** | `http://localhost/victoria-metrics/vmui/` | `game_interventions_total`, agent process metrics |

### Pre-demo checklist

- [ ] `make dry-run` came up clean: `docker compose ... ps` shows all services
      healthy (give audio + agent a few seconds to pass their health checks).
- [ ] **Gates on**: `./scripts/agent-dryrun-enable.sh status` shows
      `enabled = on`, `experiments_enabled = on`, and the `decision.backend`
      you intend to show.
- [ ] **Loop live** since the enable flip (the gate flip is live at once,
      self-gated per-tick since #1044 — no restart needed): the log shows live
      `experiment.*` activity, **not** `kill-switch: agent disabled`.
- [ ] **Remote backend reachable** (only if showing one): the agent log line
      `Decision backends registered` shows the key as set, and after
      `./scripts/agent-backend.sh claude` an `agent.decide` span in Jaeger has
      `decide.backend=claude` (not a `decide.fallback_reason`).
- [ ] **Dashboards load**: `http://localhost/` Agent + Experiments tabs render;
      Grafana **Agent Decisions & Interventions** (`/grafana/d/agent-operations`) opens.
- [ ] **Known-good flag state**: gates set as above; `scripts/flags.sh get agent`
      shows only the overlays you expect (the helper scripts write in-memory
      overlays through the flag service; no baseline file is ever written).
- [ ] *(Real controllers only)* controllers paired and showing READY (bright
      LED).
- [ ] **Panic button rehearsed**: you can flip the kill-switch off in one
      command (see act 3 / failure modes).

---

## 2. What to show — the 3-act demo arc

### Act 1 — Experiments declaring + concluding (verdicts)

The agent runs a **cohort loop**: it declares the seeded experiment, spreads
arms across **shadow** games, scores them, and reaches a **verdict**
(`significant` / `inconclusive` via a Cohen-d effect size on the objective).

- **Watch it on**: the dashboard **Experiments** tab (`http://localhost/`); in Jaeger
  (service `agent`) the `experiment.declared` → `experiment.started` →
  `experiment.game_assigned` → conclude/verdict spans.
- **Talking point**: the real game default is **never touched** — promotion gates
  (`code_improvement.*`) stay off, so a concluded experiment yields a *verdict*,
  not a live default change. The agent measures on shadow substrate first.

### Act 2 — The agent making decisions + interventions

The agent OBSERVEs the live game via OTLP telemetry, DECIDEs each cycle (rules
engine, or the remote backend selected by `decision.backend`), and — on the ACT
path — applies bounded interventions.

- **Watch it on**: the dashboard **Agent** tab (live Jaeger-polled decision
  feed) and Jaeger's `agent.decision` spans. Each decision span carries the whole
  `LayerState`: which flags were in effect, which objective was served, the chosen
  action + why, and whether it was permitted.
- **rules vs a remote backend**: every decision has an `agent.decide` span with
  `decide.backend`, the model, the confidence and the outcome (act / held /
  fallback). The span schema is the same either way, so the demo arc is identical
  (the fallback to rules is itself a talking point — see failure modes).
- **Interventions**: with the ACT path open (see the
  [ACT runbook](agent-act-runbook.md) — `interventions_enabled=on` (agent flag,
  #1213) + `enabled=on` + a permitting `interventions_allowed` variant), a permitted
  decision is written to the flag service as a game-scoped value or event and the
  coordinator applies it,
  incrementing `agent_interventions_applied_total` (agent side) and
  `game_interventions_total` (coordinator side). The dedicated **Act 2b** below
  drives interventions all the way to *applied*.
- **Drive it deterministically**: `tools/demo/demo_driver.py` plays scripted
  mock-controller movement engineered to trip a specific rule on cue — so the
  agent decides something *on demand* instead of you hoping it does. (It drives a
  real, menu-started game, not a shadow.)

### Act 2b — Interventions APPLYING (not just blocked)

This is the act that shows the agent **doing**, not just deciding. Until #1127 it
could not: the `interventions_allowed` allow-list silently resolved to `none`, so
every decision blocked `not_allowed` and **nothing ever dispatched**. The fix
matters for the demo, so it is worth narrating.

> **The #1127 fix (the reason this act now works).** `interventions_allowed` was
> a top-level **LIST** flag (each variant an array of action ids). flagd's RPC
> resolver hits the known list/object **TYPE_MISMATCH** trap (same class as
> #894/#903) and silently fails closed to the default — the agent read an empty
> allow-list and blocked **every** intervention. #1127 reshaped the variants to
> **comma-separated STRING** values (`"play_audio_cue,send_controller_effect,…"`),
> which the agent, coordinator, and menu all parse identically. The allow-list now
> actually resolves, so a permitted decision **dispatches** (`blocked=false`)
> instead of blocking `not_allowed`.

**Open all three gates, then the widest allow-list.** Gates 1+2 are the ACT path
(see the [ACT runbook](agent-act-runbook.md)); the third is the allow-list this
act widens:

```bash
# Gate 1 (sink) — agent flag `interventions_enabled`, re-read at use-time (#1213),
#   so flipping it takes effect on the next decision with NO agent restart.
scripts/flags.sh set agent interventions_enabled on
scripts/flags.sh variant agent interventions_enabled    # prints: on

# Gate 2 (kill-switch enabled=on) is handled by the enable script (live, no restart):
./scripts/agent-dryrun-enable.sh on
```

Then widen the allow-list to the **superset** so the new shadow-only levers are
permitted. Set `interventions_allowed` to `shadow_experimental` through the flag
service (a `stage` overlay, never persisted):

```bash
./scripts/talk-stage.sh permissions shadow_experimental
# or: scripts/flags.sh set agent interventions_allowed shadow_experimental
```

The **next** `agent.decision` span tags
`interventions.allowed=play_audio_cue,…,set_player_handicap,ramp_tempo,partial_shield`
(the full string, post-#1127) instead of `none`, and a permitted decision now
carries `decision.blocked=false` and completes its `agent.action` span.

**Drive a decision on cue** with `tools/demo/demo_driver.py` (scripted mock
movement trips a rule deterministically), or push synthetic metrics per the agent
README *Local smoke test*.

**The variant ladder** — each `interventions_allowed` variant is a superset of the
one above it (verbatim from `services/flagd/agent.json`):

| Variant | Adds (cumulative) | Use |
|---------|-------------------|-----|
| `none` | *(empty — nothing dispatches)* | the soft brake (act 3): loop runs + traces, every decision blocks `not_allowed` |
| `probe` | `noop` only | full ACT path without touching the game (see ACT runbook → Probe mode) |
| `ambient` *(base default)* | `play_audio_cue`, `send_controller_effect`, `adjust_volume` | safe nudges — sound/LED/volume, nothing gameplay-altering |
| `standard` | + `adjust_music_tempo`, `set_pacing_profile`, `adjust_player_sensitivity`, `grant_shield` | real-facing pacing/difficulty + the shield grace-extension |
| `full` | + `adjust_global_sensitivity`, `adjust_global_difficulty`, `eliminate_player`, `revive_player`, `end_game` | the heavy/global + lifecycle levers |
| `shadow_experimental` | + `set_player_handicap` (#1107), `ramp_tempo` (#1122), `partial_shield` (#1132) | the demo variant — adds the three **shadow-only experimental** levers on top of `full` |

> The three `shadow_experimental`-only interventions are **shadow-game-gated in
> the coordinator** as well as allow-list-gated: even with `shadow_experimental`
> set, the coordinator rejects them on a **real** game (`block_reason` on
> `game_interventions_total`). They only take effect on **shadow** substrate — by
> design, so the agent can experiment with per-player difficulty without touching
> a real party.

> **Duration-driven decisions (#1305).** Since #1305 the agent sees the real elapsed game time (before, `elapsed_s` was 0 for the whole game), so the
> duration-gated rules fire in real games. With the defaults (accelerate target 60 s, endurance minimum 120 s,
> variance window 10 s):
>
> | Elapsed | Rule | Intervention | Needs |
> |---|---|---|---|
> | under 120 s, with eliminations | R1 / R2 endurance | slow the tempo / calming audio cue | `standard` (tempo), `ambient` (cue) |
> | from 10 s | R8 statue | controller effect on a player with near-zero movement variance (held mock controllers) | `ambient` |
> | after 60 s | R5 accelerate | speed up the tempo | `standard` |
> | after 90 s (3+ players) | R6 accelerate | eliminate the least-moving player | `full` |
> | after 120 s, accelerate dominant | R7 accelerate | end the game | `full` |
>
> Jev and Claude also get the real `elapsed_s` now, so their pacing answers can change after 60/90/120 s.
> `full` (and `shadow_experimental`) lets the agent eliminate players and end the game: do not leave it set on a
> real game unless that is the point of the beat.

**What each lever does on stage** (so you can narrate the trace):

| Intervention | What it does | Bounds / notes |
|--------------|--------------|----------------|
| `grant_shield` | Extends the target's `grace_until` → **total** temporary invulnerability (death check skipped); fires a visible pulse. Real-facing (in `standard`+). | seconds > 0; **extend-only** (never shortens); writes only the agent's `grace_until`, never the admin `invincible_until` (#817). Weight: medium. |
| `set_player_handicap` | Per-player multiplicative `player_handicap_factor` on the death threshold. `>1.0` = harder to die (help); `<1.0` = easier. Composes with sensitivity. **Shadow-only.** | clamped **`[0.5, 2.0]`**, neutral `1.0`. Player-targeted (battery-gated). Weight: medium. |
| `ramp_tempo` | Scheduled music-tempo **curve** `"<target>:<seconds>:<curve>"` (e.g. `1.3:8:linear`); the 100 ms music loop interpolates toward target. **Only drives live pacing when `tempo_schedule_mode=agent`** (default `rule`). **Shadow-only.** | target clamped to `[1.0, 1.3]`; seconds `> 0`, capped **60 s**; curve in the valid set. Weight: medium. |
| `partial_shield` | Per-player **time-boxed** handicap boost — raises the death threshold hard for a window (much harder to die, **not** immune; a big spike can still kill). Distinct from `grant_shield`'s total grace. **Shadow-only.** | seconds capped **30 s**, boost `[1.0, 2.0]` (default 2.0); **extend-and-strengthen-only**; never weakens a standing handicap. Weight: medium. |

**Mind the gates that still apply** (so demo expectations are right — a blocked
decision here is *correct behaviour*, not a bug):

- **Mode-capability / shadow gate** — `set_player_handicap`/`ramp_tempo`/`partial_shield`
  only apply on **shadow** games; on a real game they block in the coordinator.
- **Battery gate** — player-targeted interventions (`grant_shield`,
  `set_player_handicap`, `partial_shield`, `adjust_player_sensitivity`) block when
  the target controller is below `policy.battery_threshold`
  (`decision.block_reason=battery_threshold`).
- **Weighted rate-limit** — the per-**game** budget is
  `policy.max_interventions_per_minute` (default 2/min); hard interventions cost 2,
  medium 1, soft 0.5. Exhaust it and the next decision blocks `rate_limit`. Raise
  to `aggressive` (4) for a busy demo. (The coordinator's separate global backstop,
  `policy.coordinator_backstop_per_minute`, only trips on a runaway agent — see the
  [ACT runbook → rate-limit ownership](agent-act-runbook.md#rate-limit-ownership--defense-in-depth-contract-919).)

**Watch it dispatch** — Jaeger and the metrics:

- Jaeger: `agent.action` spans **without** `decision.blocked` are real dispatches.
  Filter by action, e.g. `agent.action` + tag `{"decision.action":"grant_shield"}`,
  and the per-game join `{"game.id":"game_<id>"}`. The
  [trace-viz guide](agent-trace-viz.md#bonus--what-action-was-taken-agentaction)
  has the click-ready deep links.
- VictoriaMetrics (`http://localhost/victoria-metrics/vmui/`, or Grafana Explore): applied dispatches split by action
  via `sum by (action) (rate(agent_interventions_applied_total[5m]))`, and the
  applied-vs-blocked split via `agent_decisions_total{blocked="false"}` /
  `{blocked="true"}`. The Grafana **Agent Decisions & Interventions** dashboard
  ([`agent-operations.json`](../services/grafana/dashboards/agent/agent-operations.json)),
  row Interventions, shows both live, next to the coordinator-side applies.

### Act 3 — Live flag flips via the flag service (the control plane is live)

Every gate is an OpenFeature flag. A write to the flag service reaches flagd over
its gRPC sync stream (NFR-050: p95 at most 200 ms) with no restart. Show the
control plane reacting live:

- **The kill-switch panic button** — the headline live flip. Flip `enabled` off
  and the loop short-circuits on its next cycle, emitting only a throttled
  `agent.disabled` / `kill-switch: agent disabled` trace:
  ```bash
  ./scripts/agent-killswitch.sh off    # instant brake, no restart
  ./scripts/agent-killswitch.sh on     # resume
  ```
- **Swap the decision backend** (#1232) — flip who makes the `agent.decide` call
  live through the flag service, no restart. Unknown variants are refused with the
  valid list:
  ```bash
  ./scripts/agent-backend.sh                  # show backend / model / threshold / capture
  ./scripts/agent-backend.sh claude sonnet    # backend + decision.claude.model
  ./scripts/agent-backend.sh rules            # back to deterministic rules
  ./scripts/agent-backend.sh threshold strict # decision.act_confidence_threshold (claude/openai): low|gate|default|strict
  ./scripts/agent-backend.sh jev-threshold strict # decision.jev.act_confidence_threshold: inherit|gate|default|strict
  ./scripts/agent-backend.sh capture off      # GenAI content capture on|off
  ```
  API keys stay env-only (`ANTHROPIC_API_KEY`, `DECISION_OPENAI_API_KEY`,
  `JEV_API_KEY`); a backend missing its key or model falls back to rules.
- **Soften instead of stop** — set `interventions_allowed` to `none`
  (`./scripts/talk-stage.sh permissions none`): the loop still runs and traces, but every decision is blocked
  `not_allowed` (visible as `decision.blocked=true`). This is the bottom rung of
  the variant ladder in [Act 2b](#act-2b--interventions-applying-not-just-blocked);
  flipping back to `shadow_experimental` re-opens dispatch live. See the
  [ACT runbook](agent-act-runbook.md#demo-flow--drive-behavior-live-and-watch-the-trace-change)
  for objective / fitness / rate-limit live flips that each change the **next**
  `agent.decision` span.
- **Fleet view** — the Grafana **Agent Decisions & Interventions** dashboard
  (uid `agent-operations`, #791, #1427) is the wide "system is alive" shot for the
  audience: its Decide row plots decisions per minute by `decide.backend`, so a
  `./scripts/agent-backend.sh` flip shows as the stack changing colour within a
  minute (span metrics from the collector, not an agent counter).

> **Flag-change gotcha (read before the talk):** runtime flags change only
> through the flag service. The `agent-killswitch.sh` / `agent-dryrun-enable.sh`
> / `agent-backend.sh` / `talk-stage.sh` helpers and `scripts/flags.sh` do that —
> *use them.* A hand-edit of a file under `services/flagd/` changes nothing until
> `scripts/flags.sh restart`, and that restart drops every overlay (gates back
> to `off`). See failure modes → *flag changes not taking effect*.

---

## 3. Failure modes & recovery

These are the **real** failure modes hit during dry-runs. Each is *symptom →
check → fix*. The recurring theme: the agent **fails safe** (degrades to rules /
inert) rather than crashing, so the symptom is usually "nothing happened," not an
error.

### A. Remote backend not deciding → `decide.fallback_reason`

- **Symptom**: `decision.backend` is `claude` / `openai`, but the `agent.decide`
  spans show `decide.backend=rules` with a `decide.fallback_reason`.
- **Check**:
  ```bash
  docker compose logs agent | grep 'Decision backends registered' | tail -1
  ./scripts/agent-backend.sh status
  ```
  `backend_unavailable` = no key or model (export the key, recreate the agent);
  `llm_not_eligible` = a shadow game (expected; use a real game); `timeout` /
  an HTTP status in `error.type` on the child `chat` span = network or vendor
  trouble.
- **Talking point**: this degradation is *by design* — the demo keeps running on
  the rules engine. A flaky model API never takes the game down.

### B. Slow remote answers

- **Symptom**: `decide.outcome=fallback` with `decide.fallback_reason=timeout`
  on most cycles.
- **Fix**: raise `decision.claude.timeout_ms` / `decision.openai.timeout_ms`, or
  pick a faster model (`./scripts/agent-backend.sh claude haiku`). Bad venue WiFi
  is exactly the case the rules floor exists for.

### C. Experiments idle after the first one concludes

- **Symptom**: the seeded experiment runs and concludes, then the Experiments tab
  goes quiet — no new experiments appear.
- **Check**: `AGENT_EXPERIMENT_DYNAMIC_ENABLED` is `false` (the dry-run default),
  so **only the one seeded experiment** runs.
- **Fix**: that's expected. To keep a continuous stream for a longer demo, bring
  the stack up with dynamic experiment generation on:
  ```bash
  AGENT_EXPERIMENT_DYNAMIC_ENABLED=true make dry-run
  ```
  Or simply re-seed a fresh experiment on another flag before each run:
  ```bash
  AGENT_EXPERIMENT_SEED_FLAG=invincibility_seconds \
  AGENT_EXPERIMENT_SEED_VALUE=4.5 make dry-run
  ```
  (The seed flag must exist in `services/flagd/ci/game.json`, be in the
  experiment catalog of the flag service's registry, and the value must be in
  its bounds; otherwise the write is rejected with HTTP 422.)

### D. `inconclusive` verdicts on the mock substrate

- **Symptom**: experiments conclude `inconclusive` — no significant effect-size
  signal.
- **Check**: the dry-run runs on **mock controllers** with synthetic, near-uniform
  movement, so arms often look statistically indistinguishable (little real signal
  to separate them).
- **Fix**: this is *honest* and worth narrating — the framework correctly reports
  "no signal" rather than inventing one. For a guaranteed `significant` verdict on
  stage, drive differentiated movement with `tools/demo/demo_driver.py`, or seed a
  flag whose arms produce a clearly different objective on the mock substrate. Do
  **not** pretend `inconclusive` is a failure of the agent.

### E. Kill-switch off → the loop is inert

- **Symptom**: nothing happens at all — no experiments, no decisions; agent log
  shows `kill-switch: agent disabled` / `agent.disabled` spans (`agent.enabled=false`).
- **Check**: `./scripts/agent-dryrun-enable.sh status` (or `agent-killswitch.sh
  status`) — `enabled` is `off`.
- **Fix**: flip it on — the gate flip takes effect **live**, no restart
  needed:
  ```bash
  ./scripts/agent-dryrun-enable.sh on
  ```
  (Both the kill-switch *flag* and the *experiment loop* gate are read live: the
  ACT path re-reads the flag each cycle, and since #1044 the loop is always built and
  self-gates each tick on the live `experiments_enabled` flag — an off→on flip
  starts it with no agent restart. A restart/recreate is only needed when you
  set/change API keys or the `AGENT_INFERENCE_*` env vars, which are read at
  process start.)

### F. Flag changes not taking effect

- **Symptom**: you changed a flag but the agent's behavior didn't change.
- **Check**: did you edit a file under `services/flagd/` by hand? Those are the
  read-only baseline; the flag service reads them only at start. Did the write
  fail? `scripts/flags.sh` prints the HTTP status and reason (`422
  flag_read_only` for a flag outside the registry, `422 out_of_bounds`,
  `503 no_sync_stream` when flagd is not connected). Did the flag service
  restart? A restart drops every in-memory overlay.
- **Fix**: use the helper scripts or `scripts/flags.sh set <set> <flag>
  <variant>`; `scripts/flags.sh state` shows whether flagd's sync stream is
  open. For a baseline-only flag, edit the baseline file and run
  `scripts/flags.sh restart`, then re-open the gates. For rejected agent writes,
  see the
  [ACT runbook → rejected or failed intervention writes](agent-act-runbook.md#recovery--rejected-or-failed-intervention-writes).

### G. A service is unhealthy / a recovery restart

- **Symptom**: `docker compose ... ps` shows a service unhealthy, or the agent /
  flagd / Jaeger stopped responding.
- **Check**: `docker compose ... logs -f <service>`.
- **Fix**: restart just that service (the dry-run compose file set):
  ```bash
  docker compose \
    -f docker-compose.yml -f docker-compose.override.yml \
    -f docker-compose.ci.yml -f docker-compose.dry-run.yml \
    --profile agent --profile dashboard restart <service>
  ```
  After restarting **flagd**, it reconnects to the flag service and serves the
  current state (no agent restart needed). Restart the **flag service** only with
  `scripts/flags.sh restart` (it restarts flagd after it); that drops every
  overlay, so run `agent-dryrun-enable.sh on` again. After restarting the
  **agent**, re-confirm the gates are still `on` (`agent-dryrun-enable.sh status`).

### Worst case — the demo cannot recover live

If the agent / inference path is wedged on stage and a restart doesn't clear it,
**fall back to the rules engine** — it needs no LLM, no network, no hardware:

- The stack with mock controllers + `decision.backend=rules` is the
  always-available baseline (`./scripts/agent-backend.sh rules`). Re-run
  `make dry-run` + `agent-dryrun-enable.sh on` and demo acts 1–3 on rules. The
  decision arc and span schema are identical; only `decide.backend` differs.
- If the whole live demo is unrecoverable, switch to **pre-captured Jaeger traces
  / dashboard screenshots** and narrate the architecture from them. Keep a recent
  set captured during your dry-rehearsal for exactly this.

---

## 4. Teardown

Tear down the dry-run stack:

```bash
docker compose \
  -f docker-compose.yml -f docker-compose.override.yml \
  -f docker-compose.ci.yml -f docker-compose.dry-run.yml \
  --profile agent --profile dashboard down
```

The gate, backend and allow-list changes were in-memory flag-service overlays,
so `down` drops them; the baseline files were never written. To return to the
fail-closed defaults without tearing down, run `./scripts/agent-dryrun-enable.sh
off` and `./scripts/agent-backend.sh rules` (or `scripts/flags.sh reset agent
stage` and `scripts/flags.sh reset agent operator`) and confirm with
`./scripts/agent-dryrun-enable.sh status`. Unset any API keys you exported on the
host.
