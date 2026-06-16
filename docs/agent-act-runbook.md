# Agent ACT-Path Runbook — enabling the agent to act live

This runbook takes a **stock deployment** (agent observes and traces, but
dispatches nothing) to an agent that **acts on the live game**, and back. It is
the demo script for the agentic control stack (issues #722–#730).

For the design behind each flag and span, see the
[agent README](../services/agent/README.md) and
[feature-flags guide](feature-flags.md). For where to look in Jaeger/Grafana/Prometheus,
see the observability section of `.claude/rules/development.md`.

---

## TL;DR

A stock deployment dispatches **nothing**, by design. Two **independent gates**
must both be open for an intervention to reach the game:

| Gate | Where | Stock value | Act value | Restart? |
|------|-------|-------------|-----------|----------|
| **1. Sink** `AGENT_INTERVENTIONS_ENABLED` | compose env on the `agent` container | `false` (NoopActions — discards) | `true` (real `actions.Writer`) | **Yes** — restart `agent` |
| **2. Kill switch** `enabled` | `services/flagd/agent.json` flag domain | `off` | `on` | **No** — flagd hot-reload (<100 ms) |

Both gates are also the **safe defaults** when their backing config is missing
(flagd unreachable → `enabled=false`; env unset → NoopActions), so the agent
always comes up **inert**.

The kill switch is a **flag flip** — instant, no restart. That is the demo
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
- **Dispatches nothing**: the action sink is `NoopActions`
  (`AGENT_INTERVENTIONS_ENABLED` unset), so even a permitted, fitting decision is
  discarded. And `enabled=off` short-circuits the loop before any rule runs,
  emitting only a throttled `agent.disabled` kill-switch trace.

So the **whole OBSERVE/DECIDE/trace pipeline runs**, visible end to end in
Jaeger, while no intervention ever reaches the game. That is intentional — and
the reason this runbook exists.

---

## Going live — open both gates

### Step 1 — Swap in the real action sink (env, requires restart)

`AGENT_INTERVENTIONS_ENABLED=true` swaps `NoopActions` for `actions.Writer`,
which applies decisions by rewriting `services/flagd/interventions.json` in place
(the game coordinator watches that file and converges on its contents — there is
no gRPC from the agent to the game services). This env is read **once at
startup**, so it requires an agent restart:

```bash
AGENT_INTERVENTIONS_ENABLED=true docker compose up -d agent
# or set it in your .env / compose override, then:
docker compose up -d --no-deps agent
```

Confirm from the agent log:

```
Agent intervention writes enabled (#730) path=/etc/flagd/interventions.json
```

> The sink alone changes nothing yet — gate 2 (`enabled`) still short-circuits
> the loop. Both must be open.

### Step 2 — Flip the kill switch on (flag, no restart)

Edit `enabled` in `services/flagd/agent.json` — change its `defaultVariant` from
`off` to `on`:

```json
"enabled": {
  "state": "ENABLED",
  "variants": { "on": true, "off": false },
  "defaultVariant": "on"
}
```

Save. flagd's file-watch fires within **~100 ms–1 s**; no restart. The agent's
next cycle runs the rules engine instead of emitting `agent.disabled`. This is
the same mechanism documented in
[feature-flags.md → Edit Flag Values](feature-flags.md#3-edit-flag-values), and
mirrors the admin-mode `defaultVariant` rewrite in `lib/flag_config_writer.py`.

### Step 3 — Open the permission surface

Even with both gates open, the **permission layer** still bounds what dispatches.
The stock `interventions_allowed` variant is `ambient` (audio cue, controller
effect, volume only). To allow more, flip its `defaultVariant` in `agent.json`:

| Variant | Allows |
|---------|--------|
| `none` | nothing (empty allow-list) |
| `probe` | `noop` only — for [probe mode](#probe-mode-full-path-without-touching-the-game) |
| `ambient` (stock) | `play_audio_cue`, `send_controller_effect`, `adjust_volume` |
| `standard` | + tempo, pacing, player sensitivity, `grant_shield` |
| `full` | + global sensitivity/difficulty, eliminate, revive, end_game |
| `shadow_experimental` | + `set_player_handicap` (#1107), `ramp_tempo` (#1122), `partial_shield` (#1132) — **shadow-only** levers, also gated to shadow games in the coordinator |

An intervention not on the active list is **blocked** (`decision.blocked=true`,
`decision.block_reason=not_allowed`) — traced, never silently dropped.

> **The variants are comma-separated STRINGS, not arrays (#1127).** Each variant
> value is a single string like `"play_audio_cue,send_controller_effect,…"`. They
> used to be JSON arrays, which silently TYPE_MISMATCHed through flagd's RPC
> resolver and resolved to the empty default — so the agent blocked **every**
> intervention `not_allowed` and nothing ever dispatched. #1127 reshaped them to
> strings the agent/coordinator/menu all parse. If a fresh stack still shows
> everything blocked `not_allowed`, confirm the variant values are strings.

The [demo runbook → Act 2b](agent-demo-runbook.md#act-2b--interventions-applying-not-just-blocked)
walks the full ladder + the three shadow-only levers as a stage act.

---

## Kill-switch path back (instant, no restart)

Flip `enabled` back to `off` in `services/flagd/agent.json`. The next cycle
short-circuits before any rule runs; only the throttled `agent.disabled` trace is
emitted (`agent.enabled=false` visible in Jaeger). This is the **demo panic
button** — a single flag flip, hot-reloaded in <100 ms, no container restart.

(Setting `interventions_allowed` to `none` is a softer brake: the loop still runs
and traces, but every decision is blocked `not_allowed`.)

---

## Demo flow — drive behavior live and watch the trace change

Each step changes one flag live (no restart) and shows up on the **next**
`agent.decision` span in Jaeger. The decision span carries the cycle's entire
`LayerState` (#729), so a single trace answers *which flags were in effect, which
objective was served, why this action, and was it permitted*.

### A. Flip the objective → `decision.objective_served` changes

Edit `objectives.defaultVariant` in `agent.json` (e.g. `balanced_focused` →
`accelerate_focused`). On the next cycle the rules engine re-weights candidates;
the selected winner's **`decision.objective_served`** attribute on `agent.decision`
flips toward the now-dominant objective (e.g. `accelerate`). The cycle-level
`agent.objectives` attribute shows the new weight map.

### B. Change a `fitness.*` flag → `fitness.evaluated` changes

Edit e.g. `fitness.endurance.min_session_seconds` (`default` 120 → `short` 60) or
`fitness.accelerate.target_session_seconds` in `agent.json`. Fitness is evaluated
**every cycle** against the live `GameContext`, so the next decision span's
**`fitness.evaluated`** attribute shows the new threshold and recomputed progress
(e.g. `endurance.min_session_seconds=60 endurance.session_progress=…`). A failing
fitness amplifies the candidates serving that objective and can flip the selected
winner — without blocking anything outright.

### C. Observe an **applied** intervention → `game_interventions_total`

With both gates open and a permitting `interventions_allowed`, run a live game
(or push synthetic metrics — see the agent README *Local smoke test*). When the
engine selects a permitted, fitting decision:

1. `agent.action` span completes (no `decision.blocked`), and the agent rewrites
   `services/flagd/interventions.json`.
2. The **game coordinator** reads the changed file and applies the intervention,
   incrementing the Prometheus counter
   **`game_interventions_total{type, objective, blocked="false"}`**.

In Grafana/Prometheus:

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
  `conservative` (1). Once the weighted trailing-minute budget is spent, further
  decisions block with `decision.block_reason=rate_limit`.

---

## Probe mode — full path without touching the game

`AGENT_PROBE_DECISIONS=true` emits a synthetic `noop` decision every ~5 s, so you
can verify the trace pipeline without a live game. See the agent README →
[Probe mode](../services/agent/README.md#probe-mode-agent_probe_decisions).

- **Default variant**: `noop` is in no standard allow-list, so probe decisions
  are **blocked by design** (`decision.block_reason=not_allowed`). The
  `agent.signal_received → agent.decision` spans still emit — OBSERVE→DECIDE and the
  span schema are fully exercised; only `agent.action` is withheld.
- **Full path**: flip `interventions_allowed` to the **`probe`** variant
  (`["noop"]`) in `agent.json`, plus `AGENT_INTERVENTIONS_ENABLED=true` and
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
| **Jaeger** | `http://localhost:8080/jaeger/` | Decision audit traces: `agent.signal_received → agent.decision → agent.action`, and `agent.disabled` kill-switch traces. Service: `agent` |
| **Grafana** | `http://localhost:8080/grafana/` | Dashboards in `services/grafana/dashboards/` |
| **Prometheus** | `http://localhost:8080/prometheus/` | `game_interventions_total`, agent process metrics |
| **agent logs** | `docker compose logs -f agent` | Sink-enabled warning, flag evaluation, block reasons |

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

1. **Sink gate** — is `AGENT_INTERVENTIONS_ENABLED=true` on the `agent`
   container, and was the container **restarted** since? The startup log must
   show `Agent intervention writes enabled (#730)`. Without it the sink is
   `NoopActions` and discards everything (no error, no write).
2. **Kill switch** — is `enabled` `on` in `agent.json`? If `off`, the loop
   short-circuits and you'll see `agent.disabled` traces (`agent.enabled=false`),
   not `agent.decision`.
3. **Allow-list** — is the rule's intervention in the active
   `interventions_allowed` variant? Look for `decision.block_reason=not_allowed`.
4. **Battery gate** — a player-targeted decision is blocked when the target's
   battery is below `policy.battery_threshold` (`block_reason=battery_threshold`).
5. **Rate limit** — `block_reason=rate_limit` means the weighted per-minute
   budget (`policy.max_interventions_per_minute`) is spent; it replenishes over
   the trailing minute.
6. **File path** — `INTERVENTIONS_FLAG_PATH` must point at the bind-mounted file
   flagd watches (`/etc/flagd/interventions.json`). The agent is the sole writer.

### flagd unreachable → safe defaults

If flagd is down or not yet reachable at startup, **every flag evaluation falls
back to its safe default**: `enabled=false` (agent inert), and the lifecycle/
throttle flags fall back to their former hardcoded constants. The agent still
starts; it just does nothing until flagd is back. (The provider connects in the
background, so recovery needs no restart — except the read-at-startup lifecycle
flags, see the agent README → *Lifecycle flags*.)

### Rate-limit budget exhausted

Decisions blocking with `rate_limit` while the game looks quiet usually means the
budget is too low for the demo. Raise `policy.max_interventions_per_minute` to
`aggressive` (4), or recall that **hard** interventions cost 2, **medium** 1, and
**soft** 0.5 against the trailing-minute budget (see the agent README →
*Rules engine* weight table). Blocked decisions do **not** draw down the budget.

---

## Recovery — interventions file corruption (#924)

The agent applies decisions by an **in-place** read-modify-write of
`/etc/flagd/interventions.json` (no temp+rename — rename triggers EBUSY on the
docker bind mount flagd watches). A crash, an out-of-disk, or a concurrent
truncation mid-write can leave **invalid JSON**. flagd then rejects the *whole*
flag set and every intervention **silently falls back to its default** — no
write error is raised, so the legacy `agent_intervention_writes_total{result="error"}`
alert does **not** catch a previously-poisoned file.

### Built-in self-heal (automatic, no operator action)

The agent repairs corruption on its own:

1. **Validate-after-write** — after each write, the agent re-reads and re-parses
   the file. If it no longer parses, the agent **restores the last-known-good
   snapshot** (the bytes it read at the top of that write cycle, which parsed)
   in place, and the `Apply` records `result=error`.
2. **Startup self-heal** — on agent start (when `AGENT_INTERVENTIONS_ENABLED=true`),
   the agent validates the file and, if it is missing or unparseable, writes a
   **neutral document** (every flag at its neutral `defaultVariant`, empty
   targeting, no agent-driven variants — embedded in the agent binary, so it is
   always a complete, valid schema with no game effect).

Both paths increment **`agent_intervention_file_corruption_total{phase=write|startup}`**
and log `agent.intervention_file_corrupt` (with `phase`, `path`, the parse
`error`, and the `action` taken).

### Signal & alert

> **Why an agent-side counter, not a flagd metric?** flagd (scraped on
> `flagd:8014`) surfaces config-source **parse failures via logs only** — it
> exposes no scrapable sync/parse-failure counter. The agent, by contrast,
> *detects and heals* the corruption directly, so an agent-side counter is the
> fast, reliable, test-coverable signal.

Alert: **`AgentInterventionFileCorruption`** (`services/prometheus/alerts.yml`,
severity `critical`) fires immediately on any increase of
`agent_intervention_file_corruption_total`. A single corruption already defaults
every intervention until healed, so it pages without a `for:` delay.

### When the alert fires

1. **Confirm it self-healed.** The agent log shows `agent.intervention_file_corrupt`
   followed by the restore. Check the file parses:
   ```bash
   docker compose exec flagd sh -c 'cat /etc/flagd/interventions.json' | python3 -m json.tool >/dev/null && echo OK
   ```
   flagd hot-reloads the repaired file within ~100 ms–1 s; interventions resume.
2. **If it keeps firing** the underlying cause persists (agent crash-looping
   mid-write, disk full, or an external writer racing the agent — the agent must
   be the *sole* writer). Check `df -h` on the host, `docker compose logs agent`
   for restart loops, and confirm nothing else writes the bind-mounted file.
3. **Manual restore (last resort).** Re-seed the file from the canonical schema
   in the repo and restart the agent (which re-validates on startup):
   ```bash
   cp services/flagd/interventions.json /path/to/bind-mount/interventions.json
   docker compose up -d --no-deps agent
   ```
   The neutral document the agent writes is byte-identical to
   `services/flagd/interventions.json`, so this just makes the heal explicit.

The corruption is recovered automatically; the alert exists so an operator
**investigates the root cause** (why did a write get poisoned?), not so they have
to hand-repair the file.

---

## See also

- [Agent README](../services/agent/README.md) — four-layer model, span schema, action sink
- [Feature flags guide](feature-flags.md) — flagd, domains, editing flag values
- [Intervention surface research](research/722-intervention-surface.md) — the intervention vocabulary and weights
</content>
</invoke>
