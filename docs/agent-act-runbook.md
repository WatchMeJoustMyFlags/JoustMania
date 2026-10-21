# Agent ACT-Path Runbook — enabling the agent to act live

This runbook takes a **stock deployment** (agent observes and traces, but
dispatches nothing) to an agent that **acts on the live game**, and back. It is
the operator script for the agentic control stack (issues #722–#730).

For the design behind each flag and span, see the
[agent README](../services/agent/README.md) and
[feature-flags guide](feature-flags.md). For where to look in Jaeger/Grafana/VictoriaMetrics,
see the observability section of `.claude/rules/development.md`.

---

## TL;DR

A stock deployment dispatches **nothing**, by design. Two **independent gates**
must both be open for an intervention to reach the game:

| Gate | Where | Stock value | Act value | Restart? |
|------|-------|-------------|-----------|----------|
| **1. Sink** `interventions_enabled` | `agent` flag set (#1213); baseline `services/flagd/agent.json` | `off` (discards — decision recorded, not written) | `on` (real `actions.Writer` writes) | **No** — flag-service overlay, re-read at use-time |
| **2. Kill switch** `enabled` | `agent` flag set; baseline `services/flagd/agent.json` | `off` | `on` | **No** — flag-service overlay, pushed to flagd over the sync stream |

Both gates are flipped at runtime through the flag service (`scripts/flags.sh`,
or the HTTP API at `http://localhost/flags/`). The baseline file is read-only and
never written (C-033). A flip is an in-memory overlay: a flag-service restart
drops it and the gate returns to its fail-closed baseline `off`.

Both gates are also the **safe defaults** when their backing config is missing —
**fail-closed**: flagd unreachable or the flag undefined resolves to `off` for
both `enabled` and `interventions_enabled`, so the agent always comes up
**inert** (#1213).

The kill switch is a **flag flip** — instant, no restart. That is the stage
panic button.

---

## Stock deployment behavior (the two-gate baseline)

With `docker compose up`, the agent:

- **Observes**: ingests spans + metrics over OTLP (`agent:4317`), accumulates a
  `GameContext`, gates on whether a game is live with fresh data.
- **Decides**: runs the objective-weighted rules engine each gated cycle,
  evaluating all four flag layers from flagd live.
- **Traces**: emits the `agent.signal_received → agent.decision → agent.action`
  audit trace whenever the engine returns ≥ 1 decision (idle cycles cost no
  spans).
- **Dispatches nothing**: the sink gate `interventions_enabled` is `off`
  (fail-closed default), so a fully-permitted, fitting decision is recorded and
  spanned but **not written** to the flag service. And `enabled=off`
  short-circuits the loop before any rule runs,
  emitting only a throttled `agent.disabled` kill-switch trace.

So the **whole OBSERVE/DECIDE/trace pipeline runs**, visible end to end in
Jaeger, while no intervention ever reaches the game. That is intentional — and
the reason this runbook exists.

---

## Going live — open both gates

### Step 1 — Open the sink gate (flag, no restart)

Flipping `interventions_enabled` to `on` lets the gated sink dispatch through the
real `actions.Writer`, which applies decisions through the flag service's HTTP
API: a state, level or grant is a game-scoped value (`PUT /interventions/{flag}`
with `scope.game_id`, plus `scope.serial` for a player), a one-shot intervention
(`eliminate_player`, `revive_player`, `audio_cue`, `controller_effect`,
`end_game`) is an event appended to that game's event list
(`POST /interventions/{flag}`). The game coordinator evaluates these through
flagd with its `gameId` — there is no gRPC from the agent to the game services.
As of #1213 this is a **flagd flag** in the `agent` flag set, re-read
**at use-time** (per `Apply`, never cached at startup), so flipping it takes
effect on the next decision with **no agent restart**. It is **fail-closed**:
`off`, undefined, or flagd unreachable → the decision is recorded and spanned but
**not written**.

Set an `operator` overlay on `interventions_enabled` through the flag service:

```bash
scripts/flags.sh set agent interventions_enabled on
scripts/flags.sh variant agent interventions_enabled   # prints: on
```

flagd gets the change over its sync stream; no restart. The next `Apply` reads
`on` and writes through the real `Writer`. At start the agent logs the gate and
the flag-service URL it writes to:

```
Agent intervention writes gated by LIVE interventions_enabled flag (#1213; default fail-closed off) flag_service=http://flag-service:8070
```

> The sink alone changes nothing yet — gate 2 (`enabled`) still short-circuits
> the loop. Both must be open.

### Step 2 — Flip the kill switch on (flag, no restart)

```bash
./scripts/agent-killswitch.sh on      # or: scripts/flags.sh set agent enabled on
```

No restart. The agent's next cycle runs the rules engine instead of emitting
`agent.disabled`. This is the same mechanism documented in
[feature-flags.md → Edit Flag Values](feature-flags.md#3-edit-flag-values); the
menu's admin mode writes its settings through the same API.

### Step 3 — Open the permission surface

Even with both gates open, the **permission layer** still bounds what dispatches.
The stock `interventions_allowed` variant is `ambient` (audio cue, controller
effect, volume only). To allow more, set another variant through the flag
service (`scripts/flags.sh set agent interventions_allowed standard`, or
`./scripts/talk-stage.sh permissions <variant>` on stage):

| Variant | Allows |
|---------|--------|
| `none` | nothing (empty allow-list) |
| `probe` | `noop` only — for [probe mode](#probe-mode-full-path-without-touching-the-game) |
| `ambient` (stock) | `play_audio_cue`, `send_controller_effect`, `adjust_volume` |
| `standard` | + tempo, pacing, player sensitivity, `grant_shield` |
| `full` | + global sensitivity/difficulty, eliminate, revive, end_game |
| `shadow_experimental` | + `set_player_handicap` (#1107), `ramp_tempo` (#1122), `toughness_boost` (#1132) — **shadow-only** levers, also gated to shadow games in the coordinator |

An intervention not on the active list is **blocked** (`decision.blocked=true`,
`decision.block_reason=not_allowed`) — traced, never silently dropped.

> **The variants are comma-separated STRINGS, not arrays (#1127).** Each variant
> value is a single string like `"play_audio_cue,send_controller_effect,…"`. They
> used to be JSON arrays, which silently TYPE_MISMATCHed through flagd's RPC
> resolver and resolved to the empty default — so the agent blocked **every**
> intervention `not_allowed` and nothing ever dispatched. #1127 reshaped them to
> strings the agent/coordinator/menu all parse. If a fresh stack still shows
> everything blocked `not_allowed`, confirm the variant values are strings.

To walk the ladder live, repeat Step 3 with each variant and watch the `agent.action` spans without `decision.blocked=true` (see
[agent-trace-viz.md](agent-trace-viz.md)).

---

## Kill-switch path back (instant, no restart)

`./scripts/agent-killswitch.sh off` (clears the `enabled` overlay; the baseline
is `off`). The next cycle short-circuits before any rule runs; only the throttled
`agent.disabled` trace is emitted (`agent.enabled=false` visible in Jaeger). This
is the **demo panic button** — a single flag-service write, no container restart.

(Setting `interventions_allowed` to `none` is a softer brake: the loop still runs
and traces, but every decision is blocked `not_allowed`.)

---

## Demo flow — drive behavior live and watch the trace change

Each step changes one flag live (no restart) and shows up on the **next**
`agent.decision` span in Jaeger. The decision span carries the cycle's entire
`LayerState` (#729), so a single trace answers *which flags were in effect, which
objective was served, why this action, and was it permitted*.

### A. Flip the objective → `decision.objective_served` changes

`objectives` is not in the flag service's mutable registry, so this is a
baseline change: set `objectives.defaultVariant` in `services/flagd/agent.json`
(e.g. `balanced_focused` → `accelerate_focused`) and run `scripts/flags.sh
restart`. The restart drops every overlay, so open both gates again (Steps 1 and
2) afterwards. On the next cycle the rules engine re-weights candidates;
the selected winner's **`decision.objective_served`** attribute on `agent.decision`
flips toward the now-dominant objective (e.g. `accelerate`). The cycle-level
`agent.objectives` attribute shows the new weight map.

### B. Change a `fitness.*` flag → `fitness.evaluated` changes

Set `fitness.accelerate.target_session_seconds` through the flag service
(`scripts/flags.sh set agent fitness.accelerate.target_session_seconds <variant>`;
it is in the mutable registry). Other `fitness.*` flags such as
`fitness.endurance.min_session_seconds` (`default` 120 → `short` 60) are
baseline-only: edit `services/flagd/agent.json` and run `scripts/flags.sh
restart`, as in A. Fitness is evaluated
**every cycle** against the live `GameContext`, so the next decision span's
**`fitness.evaluated`** attribute shows the new threshold and recomputed progress
(e.g. `endurance.min_session_seconds=60 endurance.session_progress=…`). A failing
fitness amplifies the candidates serving that objective and can flip the selected
winner — without blocking anything outright.

### C. Observe an **applied** intervention → `game_interventions_total`

With both gates open and a permitting `interventions_allowed`, run a live game
(or push synthetic metrics — see the agent README *Local smoke test*). When the
engine selects a permitted, fitting decision:

1. `agent.action` span completes (no `decision.blocked`), and the agent writes
   the game-scoped value or event to the flag service (`flagservice.write` span).
2. The **game coordinator** evaluates the changed flag through flagd with its
   `gameId` and applies the intervention,
   incrementing the counter
   **`game_interventions_total{type, objective, blocked="false"}`**.

In Grafana (Explore, VictoriaMetrics datasource):

```promql
sum by (type) (rate(game_interventions_total{blocked="false"}[1m]))
```

### D. Observe a **blocked** one → `decision.blocked=true`

Tighten a gate and watch a block. Two easy demos:

- **Allow-list block**: set `interventions_allowed` to `none` (or a variant
  excluding the rule's intervention). The next selected decision spans carry
  `decision.blocked=true`, `decision.block_reason=not_allowed`; `agent.action` is
  **not** dispatched (recorded `blocked` on the span), and
  `game_interventions_total{blocked="true"}` increments on the game side.
- **Rate-limit block**: lower `policy.max_interventions_per_minute` to
  `conservative` (1) (`scripts/flags.sh set agent policy.max_interventions_per_minute conservative`). Once the weighted trailing-minute budget is spent, further
  decisions block with `decision.block_reason=rate_limit`.

---

## Probe mode — full path without touching the game

The flag `decision.probe_decisions` (`on`; `AGENT_PROBE_DECISIONS=true` is only its
deprecated fallback) emits a synthetic `noop` decision every ~5 s, so you
can verify the trace pipeline without a live game. It is read every cycle (no
restart) and ignored in pattern games. See the agent README →
[Probe mode](../services/agent/README.md#probe-mode-decisionprobe_decisions).

Turn it on with a flag-service overlay write: `scripts/flags.sh set agent
decision.probe_decisions on` (undo with `clear agent decision.probe_decisions`).
**Release note (FR-151, #1810):** the flag's default variant is `off` and flagd
resolves it, so a deployment that already sets `AGENT_PROBE_DECISIONS=true` no
longer probes after upgrading: the flag's default off overrides the env (the env
only applies while flagd cannot resolve the flag). Set the flag instead.

- **Default variant**: `noop` is in no standard allow-list, so probe decisions
  are **blocked by design** (`decision.block_reason=not_allowed`). The
  `agent.signal_received → agent.decision` spans still emit — OBSERVE→DECIDE and the
  span schema are fully exercised; only `agent.action` is withheld.
- **Full path**: flip `interventions_allowed` to the **`probe`** variant
  (`"noop"`) through the flag service, plus `interventions_enabled=on` and
  `enabled=on`. `noop` is a harmless no-op in the `Writer` (returns success
  without writing any flag), so the decision flows through every gate, reaches
  the real sink, and completes the `agent.action` span — all without changing the
  game.

---

## Rate-limit ownership — defense-in-depth contract (#919)

Intervention rate limiting runs in **two layers, in two languages**, with
**distinct, non-overlapping roles**. They are deliberately *not* two copies of
the same governor, and they read **different flags** so they cannot silently
drift (closes bug classes #800/#848):

| Layer | Where | Scope | Reads flag | Role |
|-------|-------|-------|-----------|------|
| **Agent (authoritative)** | `services/agent/decision/` — per-game `Loop` in `loopset.go`, limiter in `ratelimit.go` | **Per game** (`game_id`) | `policy.max_interventions_per_minute` | The real governor. Each game gets its **own** weighted per-minute budget; one game exhausting it never starves another. The agent only emits an intervention once its per-game limiter admits it. |
| **Coordinator (backstop)** | `services/game_coordinator/interventions.py` — `_RateLimiter` | **Process-global** (all games share one) | `policy.coordinator_backstop_per_minute` | A generous global safety net. Caps a *runaway/buggy* agent that floods flag writes far beyond any sane per-game rate. Tuned high enough that normal multi-session traffic never reaches it. |

**The single-owner rule (drift prevention).** Per-game pacing lives in exactly
**one** place — the agent. The coordinator does **not** re-derive or mirror the
per-game budget; it owns only the generous global flood ceiling.

- To change how aggressively a single game may be nudged → change
  `policy.max_interventions_per_minute` (agent, per-game).
- To move the global flood ceiling → change
  `policy.coordinator_backstop_per_minute` (coordinator backstop). The default
  variant (`60`/min) is intentionally far above any legitimate combined traffic;
  it only trips on an order-of-magnitude-higher, clearly-runaway write rate.

A `decision.block_reason=rate_limit` on an **agent** span means the game's own
per-game budget is spent (normal pacing). A `block_reason=rate_limited` on a
**coordinator** `game_interventions_total{blocked="true"}` increment means the
*global backstop* tripped — that is abnormal and points at a misbehaving agent,
not at normal play.

---

## Where to look

| Tool | Path | Use |
|------|------|-----|
| **Jaeger** | `http://localhost/jaeger/` | Decision audit traces: `agent.signal_received → agent.decision → agent.action`, and `agent.disabled` kill-switch traces. Service: `agent` |
| **Grafana** | `http://localhost/grafana/` | Dashboards in `services/grafana/dashboards/` |
| **VictoriaMetrics** | `http://localhost/victoria-metrics/vmui/` | `game_interventions_total`, agent process metrics |
| **agent logs** | `docker compose logs -f agent` | the `Agent intervention writes gated by LIVE interventions_enabled flag` startup line, flag evaluation, block reasons |

### Span attributes to watch (see the agent README for the full table)

| Attribute | On | Meaning |
|-----------|----|---------|
| `agent.enabled` | every span | gate 2 (`false` = kill switch on) |
| `agent.objectives` | `agent.decision` | live objective weight map |
| `interventions.allowed` | `agent.decision` | active allow-list (`none` / `a,b,c`) |
| `decision.objective_served` | `agent.decision` | objective the selected rule served |
| `decision.action` / `decision.reason` | `agent.decision` | the chosen intervention + why |
| `decision.blocked` / `decision.block_reason` | `agent.decision` | `not_allowed` / `battery_threshold` / `rate_limit` |
| `fitness.evaluated` | `agent.decision` | per-objective thresholds + computed progress |

Cross-reference: [agent README → Span schema (#724)](../services/agent/README.md#span-schema-724--the-trace-is-the-audit-log).

---

## Troubleshooting

### "The agent decides, but nothing happens in the game"

Walk the two gates plus the permission layer, in order:

1. **Sink gate** — does `scripts/flags.sh variant agent interventions_enabled`
   print `on` (#1213)? It is re-read **per `Apply`**, so a flip takes effect with
   no restart; `off`/undefined/flagd-unreachable is **fail-closed** and discards
   everything (decision recorded, no write — no error). A flag-service restart
   drops the overlay and returns the gate to `off`.
2. **Kill switch** — does `scripts/flags.sh variant agent enabled` print `on`? If `off`, the loop
   short-circuits and you'll see `agent.disabled` traces (`agent.enabled=false`),
   not `agent.decision`.
3. **Allow-list** — is the rule's intervention in the active
   `interventions_allowed` variant? Look for `decision.block_reason=not_allowed`.
4. **Battery gate** — a player-targeted decision is blocked when the target's
   battery is below `policy.battery_threshold` (`block_reason=battery_threshold`).
5. **Rate limit** — `block_reason=rate_limit` means the weighted per-minute
   budget (`policy.max_interventions_per_minute`) is spent; it replenishes over
   the trailing minute.
6. **Flag-service write rejected** — the flag service's `flagservice.write` span
   (Jaeger) or the agent log carries the reason: `409 game_not_live` (the coordinator has not registered
   the game, or it ended), `422 out_of_bounds` / `flag_read_only`, or
   `503 no_sync_stream` (flagd is not connected; `scripts/flags.sh state`).
   `flagservice_writes_total{result="rejected"}` counts them by `reason`.

### flagd unreachable → safe defaults

If flagd is down or not yet reachable at startup, **every flag evaluation falls
back to its safe default**: `enabled=false` (agent inert), and the lifecycle/
throttle flags fall back to their former hardcoded constants. The agent still
starts; it just does nothing until flagd is back. (The provider connects in the
background, so recovery needs no restart — except the read-at-startup lifecycle
flags, see the agent README → *Lifecycle flags*.)

### Rate-limit budget exhausted

Decisions blocking with `rate_limit` while the game looks quiet usually means the
budget is too low for the stage run. Raise `policy.max_interventions_per_minute` to
`aggressive` (4), or recall that **hard** interventions cost 2, **medium** 1, and
**soft** 0.5 against the trailing-minute budget (see the agent README →
*Rules engine* weight table). Blocked decisions do **not** draw down the budget.

---

## Recovery — rejected or failed intervention writes

Since the flag service (epic #1407) the agent writes no file, so the
interventions-file corruption of #924 (and its self-heal and
`agent_intervention_file_corruption_total` counter) no longer exists. The flag
service validates every write against its registry and answers with a status
and a reason code; a rejected write changes nothing.

### Signal

Watch `flagservice_writes_total{flag_set="interventions",result="rejected"}`
by `reason` (Grafana **Flags / OpenFeature** dashboard, uid `feature-flags`, "Flag service" row) and the
`flagservice.write` spans in Jaeger. No Prometheus alert ships for it (alerting
removed in #1425, FR-083 Rejected).

### When rejections increase

1. **`game_not_live`** — the game coordinator registers each game
   (`PUT /games/{id}`) before it runs and ends it at the end; check
   `scripts/flags.sh state` (live games) and the coordinator log. The agent
   retries a `game_not_live` write once after 250 ms.
2. **`no_sync_stream`** — flagd is not connected to the flag service. Check
   `docker compose logs flagd flag-service`; `scripts/flags.sh restart` restarts
   both in order.
3. **`out_of_bounds` / `flag_read_only`** — the agent asked for a value outside
   the registry ([flags.md §13](specs/flags.md#13-mutable-flag-registry)); a
   code or registry bug to file, not an operator fix.

A game's intervention values and events die with the game (UC-053 BR-003), so
there is nothing to restore by hand.

---

## See also

- [Agent README](../services/agent/README.md) — four-layer model, span schema, action sink
- [Feature flags guide](feature-flags.md) — flagd, domains, the flag service, changing flag values
- [Intervention surface research](research/722-intervention-surface.md) — the intervention vocabulary and weights
</content>
</invoke>
