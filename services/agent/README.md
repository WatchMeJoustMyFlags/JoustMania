# Agent

Adaptive-difficulty agent that turns live telemetry into intervention decisions.

## Overview

The Agent sits between JoustMania's **observation layer** (OpenTelemetry) and its
**control layer** ([OpenFeature](https://openfeature.dev/) / flagd). It receives
spans and metrics from the OTel Collector over OTLP, accumulates a rolling
`GameContext`, gates on whether the context is worth acting on, and calls stub
decision hooks that will eventually drive feature-flag changes.

The Collector fans telemetry out to the Agent via an `otlp/agent` exporter wired
into the existing **traces** and **metrics** pipelines — the Agent receives the
same signals as the rest of the observability stack, no new instrumentation
required.

Written in Go using the OpenTelemetry Collector `pdata` libraries and standard
`net/http` for health checks.

### Signal timing (design note)

**Metrics are the primary, timely signal source.** Counter/gauge updates reach
the Agent in roughly **100 ms – 1 s** end to end, so difficulty decisions are
driven by metrics.

**Spans are late by design.** `player_lifecycle` spans only flush at game end,
and even then sit behind the ~10 s trace batch window. They are used for
**audit/confirmation** of decisions, never as the timely trigger.

The metric extractor already recognizes **both** today's metric names **and** the
five #722-proposed metrics, so each signal lights up automatically the moment its
producer ships:

- `game_player_movement_variance`
- `game_player_skill_level`
- `controller_battery_pct`
- `game_player_elimination_order`
- `game_interventions_total`

## GameContext

The Agent accumulates context at two scopes.

| Scope | Fields |
|-------|--------|
| **Per-player** | `movement_intensity`, `movement_variance`, `battery_pct`, `skill_level`, `active` |
| **Per-session** | `duration_seconds`, `active_player_count`, `elimination_sequence` |

### Session identity heuristic

Sessions are not carried in every signal, so the Agent synthesizes them:

- A new synthetic session (`session-N`) is created when `game_active` transitions
  **0 → 1**.
- Once a `game_id` label is observed, the synthetic session **adopts** that id.

### Staleness & eviction

| Entity | Rule |
|--------|------|
| Player | Evicted after a **5 s** TTL with no fresh signal |
| Session | Kept for a **15 s** grace period after it goes inactive, then evicted |

## Per-game partitioning (#845)

With `GAME_MAX_CONCURRENT_GAMES` allowing several games at once, the Agent
partitions everything stateful **by `game_id`** so two concurrent games never
share player state, a session, or a decision budget.

### Context: the Multiplexer (PR B)

The `gamecontext.Multiplexer` holds **one `Store` partition per `game_id`**. Each
received datapoint/span is routed to its partition on the resolved `game_id`
**before** it is applied, so players, sessions, and elimination order stay
disjoint. Signals that carry no `game_id` land on the **fallback partition**
(`FallbackGameID = ""`) — the zero-regression linchpin: in single-game mode (or
against a coordinator without the `game_id` labels) every signal lands there and
the Multiplexer behaves byte-for-byte like the old single `Store`.

Partitions are created **lazily** on first touch and removed by `EvictStale` once
a game's session has ended past grace and retains no fresh players. The fallback
partition is **never** wholesale-removed (its session still resets on grace).

### Decisions: the LoopSet (PR C)

The `decision.LoopSet` is the per-game counterpart to the Multiplexer: **one
`decision.Loop` per partition**, lazily created on first evaluation. Each Loop
owns its:

- **weighted rate-limit budget** (`policy.max_interventions_per_minute`) — one
  game exhausting its per-minute budget does **not** block another game;
- **log/span throttle slot** (`decision.throttle_seconds`) — one game's throttle
  never silences another's `agent.evaluate` log or `agent.disabled` span;
- **per-cycle `LayerState`** (the `#729` span-attribution source of truth).

**Shared vs per-game wiring.** The factory (`main.go`) builds, per Loop:

| Component | Per-loop? | Why |
|-----------|-----------|-----|
| Rules engine (`ObjectiveRules`) | **Fresh per loop** | Carries per-cycle mutable state the loop drives every cycle — `SetObjectives`/`SetFitness` published before `Evaluate`, `LastFitness` read after. A shared engine would let two games **race** each other's objective weights and fitness reads, stamping the wrong game's `agent.objectives`/`fitness.evaluated` onto a span. |
| Action sink (`actions.Writer`) | **Shared** | Holds no per-game state — it serializes a read-modify-write of the flagd interventions file under its own mutex, keyed by the decision's target. Safe to close over one instance. |
| Flag source, tracer | **Shared** | Stateless / concurrency-safe. |

### Loop lifecycle / eviction

The eviction ticker runs `mux.EvictStale()` and then
`loops.Retain(mux.Partitions())`: a removed partition's Loop — and its budget +
throttle state — is dropped **in lockstep** with its Store. The **fallback loop
is permanent** (`Retain` skips it), mirroring the fallback Store. A dropped
game's Loop is **recreated lazily with a fresh budget** if the game resumes —
acceptable because a partition is only dropped after its game fully ended past
grace.

### Span attribution

Decision spans are independently attributable per game. The root
`agent.span_received` span carries both `session.id` and the `game.id` alias
(`session.id` **is** the real `game_id` since PR A's early adoption); the
decision / `agent.disabled` / `agent.llm.prompt` spans carry `game.kind` (and the
prompt-capture span carries `game.id` too). So a Jaeger query by `game.id` is
symmetrical with the coordinator's own `game.id`-tagged spans, and two concurrent
games' traces never blur together.

### Multi-game decision flow

```
OTLP Export (metrics/traces batch)
        │  per datapoint/span: resolve game_id
        ▼
  Multiplexer ── route ──► Store[game-A]      Store[game-B]      Store[""]   (fallback)
        │                      │                  │                 │
        │  touched game_ids    │ Snapshot         │ Snapshot        │ Snapshot
        ▼                      ▼                  ▼                 ▼
  signalUpdated ──► gate.ShouldEvaluate(snap) per partition
                            │ (if eligible)
                            ▼
                 LoopSet.For(game_id) ──► Loop[game-A]   Loop[game-B]   Loop[""]
                                              │  own budget   │ own budget   │ own budget
                                              │  own throttle │ own throttle │ own throttle
                                              ▼               ▼              ▼
                                        agent.decision spans (game.id / game.kind)
```

> **#847 hook:** the per-game LLM gating (per-game cadence / eligibility) will
> attach to this per-game Loop state; the **global** LLM budget stays shared
> across loops (one inference-cost ceiling for the whole agent).

## Infrastructure observe path (#733)

Alongside the game `GameContext`, the Agent runs a **parallel infrastructure
observe path** over the same OTLP trace receiver. It recognizes the periodic
`controller.bluetooth_health` span emitted by controller-manager (PR #785),
extracts the Bluetooth transport signals into a thread-safe `InfraContext`
(`infracontext/`), and triggers an infrastructure evaluation loop.

This PR is **OBSERVE only**: the loop is a logging stub
(`decision.InfraLoop`, debug level, throttled to ~1/sec). Fitness, decisions, and
remediation land in later stacked PRs behind the `decision.InfraEvaluator` seam.
The telemetry constants those PRs converge on — span `agent.infrastructure.decision`
and its attribute keys — are already declared in `decision/infra_telemetry.go`
(no span is emitted yet).

### Input span contract (`controller.bluetooth_health`)

A ~1Hz root span; **absent when no controllers are connected**.

| Span attribute | Type | Meaning |
|----------------|------|---------|
| `bluetooth.event_gap_ms` | float | window max inter-fresh-frame gap (ms) |
| `bluetooth.dropped_events_pct` | float 0–1 | window drop ratio |
| `bluetooth.movement_update_hz` | float | window min per-serial fresh rate |
| `bluetooth.active_controllers` | int | active controller count |
| `bluetooth.target_backend` | string | rollout target adapter_type (`""` when off) |
| `bluetooth.rollout_count` | int | current rollout controller count (`0` when off) |

One `bluetooth_controller_sample` **span event per active serial** carries the
per-controller view: `controller.serial`, `bluetooth.movement_update_hz`,
`bluetooth.dropped_events_pct`, `controller.adapter` (winning adapter_type, e.g.
`python`/`rust`/`unstable`). Numeric attributes are read tolerant of both Int and
Double encodings; any missing window attribute simply stays `nil` (the extractor
sets a pointer only for present attributes).

### InfraContext lifecycle

- **Window signals** are replaced **wholesale** on each health span — an absent
  attribute next window drops back to `nil`, never a stale carry-over.
- **Per-controller** records accumulate keyed on serial; each appearance stamps
  `LastUpdate`.
- **Eviction**: a controller absent from health spans past a **5 s** TTL (≈ five
  missed 1Hz windows) is dropped, on the same eviction ticker as the game store.
- `Snapshot()` deep-copies the controllers map, so the stub loop reads an
  isolated view (the per-span pointer-replacement invariant means later spans
  never mutate an already-handed-out snapshot).

The same **self-ingestion skip** as the game path applies: any resource whose
`service.name` equals the agent's own `OTEL_SERVICE_NAME` is ignored on the infra
path too. A mixed batch (game spans + health spans) feeds both stores
independently with no cross-contamination.

### Infrastructure fitness (#735)

`decision.EvaluateInfraFitness(InfraContext, BluetoothThresholds)` scores the
Bluetooth transport against three **flag-sourced** thresholds and returns a
structured `InfraFitnessResult` (`Evaluated` / `Passing` / `Violations` /
`Values`). This is the infra-domain parallel to the game fitness functions
(#731); it makes no rollout decision itself — rollout **expansion** (#734,
[below](#progressive-rollout-expansion-734)) consumes the result, and
**rollback** (#736) plugs into the same loop.

| Fitness function | Flag key (default) | Violated when |
|------------------|--------------------|---------------|
| event gap | `fitness.bluetooth.max_event_gap_ms` (50) | `Window.EventGapMs > threshold` |
| dropped events | `fitness.bluetooth.max_dropped_events_pct` (0.02) | `Window.DroppedEventsPct > threshold` |
| movement rate | `fitness.bluetooth.min_movement_update_hz` (10) | an update rate `< threshold`, evaluated at both window-min and **per-controller** granularity |

**Live-tunable:** the thresholds are read from the `fitness.bluetooth.*` flags on
**every** infra observe cycle through a `decision.BluetoothFitnessSource`. In
production that source is `decision.FlagBluetoothFitness`, which is backed
**directly by the flags client** (`flags.Flags.BluetoothFitness`, a narrow
accessor that evaluates only the three `fitness.bluetooth.*` flags, not the full
four-layer agent `Snapshot`). The infra loop re-reads it once per ~1Hz cycle, so
a threshold flipped on stage takes effect on the **next** evaluation with no
restart — and it does so **in the lobby too**, independent of whether a game is
active (the game decision loop only publishes thresholds while a session runs, so
it cannot keep the infra loop live on its own). On any evaluation error the
accessor falls back to the flagd-schema defaults, so a down flagd reverts to safe
thresholds rather than failing the cycle. This source is **separate** from the
game-objective `FitnessSource` — distinct flags, distinct concerns. (A
`decision.LiveBluetoothFitness` push-source also exists, seeded with the defaults,
used as a static seed in tests.)

**Missing signals are skipped, not failed** (mirroring game fitness): a `nil`
window signal contributes no violation, and a context with *no* window signals at
all returns `Evaluated=false`. The movement-rate dedup: when the window min **and**
a specific controller both breach the hz floor, only the **per-serial**
violation(s) are emitted (each names its offending serial); the window-level hz
violation fires **only** when no per-controller rate is available to attribute it.

**Violation string** (`InfraFitnessResult.ViolationsString()`) is the stable,
deterministic (sorted-serial) form PR F/G lifts onto the `fitness.violations` span
attribute — `"<signal>[<serial>] <observed><cmp><threshold>"`, joined by `"; "`:

```
event_gap_ms 87.5>50; movement_update_hz[AA:BB] 8.3<10
```

### Progressive rollout expansion (#734)

`decision.InfraLoop` consumes the infra fitness result to **expand the Bluetooth
backend rollout controller-by-controller**. When fitness passes it advances
`rollout.current_controller_count` one stage up a fixed ladder by rewriting
`services/flagd/rollout.json` (the controller-manager watches the file and
converges on it). The agent is the **sole writer** of `rollout.json`, via the
same order-preserving in-place RMW as the interventions writer
(`actions.RolloutWriter`); untouched flags round-trip byte-for-byte.

**Stage ladder** (`current_controller_count` variants):

| Variant | `none` | `one` | `three` | `six` | `all` |
|---------|--------|-------|---------|-------|-------|
| Value   | 0      | 1     | 3       | 6     | 99    |

flagd flips by **variant name**, not value, so the loop maps the observed count
(a number, read from the `controller.bluetooth_health` span's
`bluetooth.rollout_count`) to the next variant via `actions.NextStage` /
`actions.StageVariantForValue`. `all` (99) is terminal.

**Decision matrix** (per observe cycle; observed state comes from the health
span, **not** a flag re-read):

| Rollout state | Fitness | Allowed / Dwell-or-hold | Action | `remediation.action` | Span? |
|---------------|---------|--------------------------|--------|----------------------|-------|
| inactive (`target_backend==""`) | — | — | nothing | — | **no span** |
| active, stage `< all` | passing | dwell elapsed, not held | flip to next variant | `expand` | yes |
| active, stage `< all` | passing | dwell not elapsed / held (cooldown) | nothing | `none` | yes |
| active, stage `== all` | passing | — | nothing (terminal) | `none` | yes |
| active, stage `> none` | **failing** | **allowed** | reset to `none` (roll back) | `rollback` | yes |
| active, stage `> none` | **failing** | **not allowed** | nothing (recommend) | `recommended_only` | yes |
| active, stage `> none` | **failing** | rollback already in flight (settling) | nothing | `none` | yes |
| active, stage `== none` | **failing** | — | nothing (nothing to roll back) | `none` | yes |
| active | write error | — | attempted, failed | `expand` / `rollback` (+ span error/status) | yes |

See [Auto-remediation / rollback](#auto-remediation--rollback-736) for the
failing-fitness rows.

**Dwell:** after each expansion the loop waits `AGENT_ROLLOUT_DWELL_SECONDS`
(default **15s**) before expanding again, so fitness has time to observe the
newly-added controllers at the current stage. The first expansion (no prior
`lastExpansion`) is not delayed. A failed write does **not** advance the dwell
clock, so the next passing cycle retries.

**Freshness gate** (`gate.ShouldEvaluateInfra`): the loop only evaluates when
≥1 controller reported fresh `controller.bluetooth_health` within the controller
TTL (5s). It is **game-state-independent** — controllers connect and stream in
the lobby, and the rollout is driven by transport health alone.

**Span** — `agent.infrastructure.decision`, emitted on **every active-rollout
cycle**, carries `rollout.target`, `fitness.passing`, `fitness.violations`
(empty when passing), `remediation.action`, `rollout.dry_run` (whether the
decision was only rehearsed, not applied), the observed `bluetooth.*` signals,
and `rollout.controller_count` (the new stage value) when expanding. A write
failure sets the span status to error and records the error.

**Env gates:**

| Env | Default | Effect |
|-----|---------|--------|
| `AGENT_ROLLOUT_ENABLED` | `false` | `true` → real `RolloutWriter` applies flips (`rollout.dry_run=false`). `false` → **dry-run**: the loop still decides and spans expansions (`remediation.action="expand"`, `rollout.controller_count` set, **`rollout.dry_run=true`**) but does **not** write `rollout.json` (decided-but-not-applied; logged `agent.rollout_dry_run`). |
| `ROLLOUT_FLAG_PATH` | `/etc/flagd/rollout.json` | rollout flag file path (read for `remediation_allowed`, written on expand/rollback) |
| `AGENT_ROLLOUT_DWELL_SECONDS` | `15` | per-stage dwell before re-expansion |
| `AGENT_ROLLOUT_COOLDOWN_SECONDS` | `30` | post-rollback cooldown: re-expansion is suppressed this long after a rollback |

### Auto-remediation / rollback (#736)

When infrastructure fitness **fails** during an active rollout, the loop closes
the remediation loop: it can **roll the backend rollout back** by resetting
`current_controller_count` to `none` (value 0), pulling **all** controllers back
to the stable default backend. `target_backend` is **left untouched** — that
preserves the operator's intent for the next attempt; only the controller count
is wound back.

**The loop:**

```
if not fitness.passing and stage > none:
    if remediation_allowed:
        rollback_backend()                 # writes current_controller_count = "none"
        span: remediation.action = "rollback"
        start cooldown                     # suppress re-expansion for AGENT_ROLLOUT_COOLDOWN_SECONDS
    else:
        span: remediation.action = "recommended_only"   # no write
```

**`remediation_allowed` gating.** Auto-rollback is gated on the
`remediation_allowed` flag. That flag lives in the **`rollout` flagd domain**
(`services/flagd/rollout.json`, `flagSetId: "rollout"`), **not** the agent
domain. The agent already owns that file (it is the sole writer of
`rollout.json`), so it resolves the gate by reading the flag's `defaultVariant`
**directly from that document** each cycle (`actions.RemediationReader`), rather
than standing up a second flagd RPC domain client. Any read/parse error →
**`false`** (recommend-only) — the fail-closed safe default. Flip the flag's
`defaultVariant` to `on`/`off` to enable/disable auto-rollback live (no restart).

**Cooldown.** After a successful rollback the loop starts a cooldown
(`AGENT_ROLLOUT_COOLDOWN_SECONDS`, default **30s**), wired through the
`holdExpansion` hook. While in cooldown the loop will **not** re-expand even if
fitness recovers — otherwise it would roll back, immediately see passing fitness
at stage `none`, and climb straight back into the same failure. The cooldown is
held **in-memory** (`cooldownUntil`), so restarting the agent process clears it
(a demo reset path).

**Span value set & the settling window.** `remediation.action` is a **closed
set** of exactly four values — `none | expand | rollback | recommended_only`.
The rollback is written **once per failure episode**: after the write, the
controller-manager takes several windows to converge `current_controller_count`
back to 0. During that **settling window** (rollback written, observed
`bluetooth.rollout_count` still `> 0`), further failing cycles record
**`none`** (with the populated `fitness.violations`) and do **not** re-write —
re-emitting `rollback` without a write would lie. The episode ends when the
observed count returns to 0; a later failure can then trigger a fresh rollback.

| Failing-fitness state | observed `rollout_count` | allowed | write | `remediation.action` |
|------------------------|--------------------------|---------|-------|----------------------|
| active stage           | `> 0`, no rollback in flight | yes | `current_controller_count="none"` | `rollback` |
| active stage           | `> 0`, no rollback in flight | no  | none | `recommended_only` |
| rollback in flight (settling) | `> 0` | — | none | `none` |
| at stable default      | `0` | — | none (nothing to roll back) | `none` |

**Dry-run** (`AGENT_ROLLOUT_ENABLED=false`): the rollback is **decided and
spanned** (`remediation.action="rollback"`, **`rollout.dry_run=true`**) but the
`DryRunRolloutWriter`'s `SetControllerCount("none")` is a no-op —
decided-but-not-applied, consistent with dry-run expansion. The `rollout.dry_run`
attribute is what lets a Jaeger consumer tell this rehearsal apart from a real
rollback (the spans are otherwise identical). A rollback **write error** sets the span status to error,
records the error, and does **not** mark the episode in flight, so the next
failing cycle retries (mirrors the expansion error path).

**Demo reset.** Flip `remediation_allowed` to `off` to stop auto-rollback (the
loop drops back to `recommended_only`); restart the agent to clear an in-progress
cooldown.

### Incident narrative (#737) — the rollout story is auditable in traces

Every active-rollout cycle emits one `agent.infrastructure.decision` span, and the
**ordered timeline of those spans reconstructs the whole incident** — rollout
expanded → transport degraded → fitness violated → rollback executed → fitness
recovered → re-expanded — with no extra logging. The spans are built by a **single
attribute builder** (`infraDecisionAttributes`), so **every** decision span — on
**every** outcome — carries the same five core attributes:

| Attribute | Type | On every span | Meaning |
|-----------|------|---------------|---------|
| `rollout.target` | string | yes | the rollout target adapter in effect |
| `fitness.passing` | bool | yes | did the Bluetooth fitness check pass this cycle |
| `fitness.violations` | string | yes (**empty when passing**, never absent) | the failing checks, e.g. `movement_update_hz[AA:BB] 8.3<10` |
| `remediation.action` | string | yes | `none \| expand \| rollback \| recommended_only` |
| `rollout.dry_run` | bool | yes | did the actuator only **rehearse** this decision (no write to `rollout.json`) rather than apply it — `true` for the dry-run actuator (`AGENT_ROLLOUT_ENABLED=false`, the compose default) and when there is no actuator; `false` for the real `RolloutWriter`. Lets a trace tell a rehearsed `expand`/`rollback` apart from a real one. |

plus the observed `bluetooth.*` window signals (`target_backend` + `rollout_count`
always; `event_gap_ms` / `dropped_events_pct` / `movement_update_hz` /
`active_controllers` when present in the window), and `rollout.controller_count`
(the stage expanded to) **only** on an `expand`.

`fitness.violations` is **present-but-empty** on a passing cycle, never dropped —
so a trace query can tell "passing" apart from "attribute missing".

**The span timeline of a full episode** (read top-to-bottom in Jaeger):

| # | cycle | `remediation.action` | `fitness.passing` | `fitness.violations` |
|---|-------|----------------------|-------------------|----------------------|
| 1 | rollout active, healthy | `expand` (`→1`) | true | *(empty)* |
| 2 | dwell elapsed, still healthy | `expand` (`→3`) | true | *(empty)* |
| 3 | transport degrades | `rollback` | false | non-empty |
| 4 | settling (count still > 0) | `none` | false | non-empty |
| 5 | reset landed, recovered, in cooldown | `none` | true | *(empty)* |
| 6 | cooldown elapsed, healthy | `expand` (`→1`) | true | *(empty)* |

The `recommended_only` variant (operator has **not** set `remediation_allowed`)
stalls at step 3: the loop records `recommended_only` (with the violations) and
never writes, so the story does not progress to recovery.

**Query it in Jaeger.** The observability stack is at `http://localhost:8080/`
with Jaeger under the `/jaeger` base path:

- UI: select **service `agent`**, **operation `agent.infrastructure.decision`**,
  order by time, and read the `remediation.action` timeline down the trace list.
- Jaeger v2 API (note the `/jaeger` prefix):
  `http://localhost:8080/jaeger/api/traces?service=agent&operation=agent.infrastructure.decision`
  (or `http://jaeger:16686/jaeger/api/traces?service=agent` from inside the
  compose network). Filter for `remediation.action=rollback` to jump straight to
  the remediation events.

The Go narrative test (`decision/infra_narrative_test.go`,
`TestInfraNarrative_FullIncident`) drives this exact sequence with a fake
clock/actuator/remediation and asserts the timeline; the schema-completeness test
(`TestInfraSchemaCompleteness_AllOutcomes`) asserts the five core attributes are
present on **every** outcome, catching any future emission path that forgets the
builder.

## Gating & decisions

After each context update the Agent evaluates `should_evaluate`. When the gate
opens it runs the decision loop, which evaluates the **OpenFeature** control
flags from flagd on **every cycle** (never cached). The flags form a **four-layer
model**, applied in order:

### The four flag layers

1. **Existence** (gates the loop) —
   - `enabled` (bool): the kill switch. When `false` the loop short-circuits
     immediately, before any rules run or spans are emitted. This is also the
     safe default when flagd is unreachable, so the agent comes up inert.
   - `mode` (string): selects the decision path. `rules` runs the deterministic
     rules engine; `llm` is reserved for M4 and currently logs a note (including
     the capability selection) and falls back to rules.
2. **Objective** (steers the rules) —
   - `objectives` (object → `map[string]float64`): per-session goal weights. The
     loop publishes the per-cycle value into the rules engine through a
     `LiveObjectives` source (`decision/objectives.go`); the engine reads it
     inside `Evaluate`, falling back to `{endurance: 1.0}` when the flag resolves
     nothing. This replaces the engine's static objective source (#726).
   - `fitness.*` (numbers): the per-objective fitness thresholds (#731). The loop
     publishes the per-cycle values into the engine through a `LiveFitness`
     source (`decision/fitnesssource.go`); the engine evaluates the fitness
     functions against the live game context each cycle and amplifies the
     candidates serving a *failing* objective. See **[Fitness functions
     (#731)](#fitness-functions-731)** below.
3. **Capability** (selects the model/prompt for the M4 LLM path) —
   - `model` (string, default `phi4-mini`) and `prompt_variant` (string, default
     `conservative`). Evaluated and **recorded** every cycle; not consumed until
     the M4 LLM path lands. They are passed along the `llm` path stub.
4. **Permission** (constrains which actions dispatch, and how fast) — applied to
   each candidate decision in this order, blocking with an attributed reason:
   - `interventions_allowed` (object → `[]string`): the allow-list gate (#727).
     A decision whose intervention is not on the list is blocked
     (`reason=not_allowed`). An empty allow-list dispatches nothing.
   - `policy.battery_threshold` (int %, default 20): a **player-targeted**
     decision is blocked (`reason=battery_threshold`) when the target player's
     battery is below the threshold — a low-battery controller signals unreliable
     input. Session-scoped decisions are unaffected; missing battery data is
     treated as unknown (does not block, but is noted).
   - `policy.max_interventions_per_minute` (int, default 2): a **weighted
     sliding-window** rate limiter across all dispatched interventions. Weights:
     soft (`play_audio_cue`, `send_controller_effect`, `adjust_volume`) = 0.5;
     medium (`adjust_music_tempo`, `adjust_player_sensitivity`, `grant_shield`) =
     1; hard (`adjust_global_sensitivity`, `eliminate_player`, `revive_player`,
     `end_game`) = 2 (see `docs/research/722-intervention-surface.md` §5). When
     the budget for the trailing minute is exhausted, further decisions are
     blocked (`reason=rate_limit`), not queued.
   - `policy.movement_variance_window` (int seconds, default 10): evaluated and
     **recorded in the LayerState**. The rules engine reads the window from its
     `PolicySource` (flagd-schema default); the recorded value is what #731's
     variance logic and #729's span attribution consume.

Every value evaluated this cycle plus the per-decision outcomes (dispatched /
blocked + reason, with the rate-limit weight charged) are captured in a single
cohesive **`LayerState`** (`decision/layerstate.go`), returned from `OnEvaluate`
and retained via `LastLayerState()`. It is the span-attribute **source of truth**
that #729 lifts onto the decision span verbatim.

The two decision hooks:

- **Rules engine** (#726) — `ObjectiveRules`, the objective-weighted decision
  logic below. Active by default; its objectives are driven live by the flag.
- **Action sink** (#730) — applies permitted intents via OpenFeature/flagd.
  **No-op by default**: with `AGENT_INTERVENTIONS_ENABLED=true` the real
  `actions.Writer` is wired in and dispatched decisions are written to the
  flagd `interventions` domain (see **Action sink** below); otherwise decisions
  are traced and discarded.

The flags wrapper lives in [`flags/`](flags/) and uses the OpenFeature Go SDK
with the flagd **RPC** resolver against flagd's gRPC evaluation port. Flag keys
are flat (`enabled`, `mode`, `objectives`, `interventions_allowed`) and match
[`services/flagd/agent.json`](../flagd/agent.json) (flagSetId `agent`). See
[`docs/research/722-intervention-surface.md`](../../docs/research/722-intervention-surface.md)
for the intervention-surface design.

## Rules engine (#726)

`ObjectiveRules` is the `rules_decide(context, objectives)` path — the non-LLM
intelligence and the final link of the inference fallback chain. Each rule
yields candidates with an urgency (0–1) and the objective they serve; the
final score is `urgency × weight[objective]`. Candidates below a minimum score
(0.10) are dropped, the rest are admitted best-first (ties: cheaper first, then
name) — at most 2 decisions per evaluation, and the rule set runs at most once
per second. The weighted per-minute budget is enforced downstream by the loop
(see the permission layer above), not by the engine.

| Rule | Objective | Trigger | Intervention |
|------|-----------|---------|--------------|
| R1/R2 | endurance | session younger than `fitness.endurance.min_session_seconds` while players are eliminated | `adjust_music_tempo` (slow) / `play_audio_cue` |
| R3 | balanced | skill spread > `fitness.balanced.max_skill_gap` | `adjust_player_sensitivity` → highest-skill outlier |
| R4 | balanced | weakest player while the field shrinks (needs ≥ 2 players with known skill — "weakest" is only meaningful relative to others) | `grant_shield` → weakest |
| R5 | accelerate | duration > `fitness.accelerate.target_session_seconds` | `adjust_music_tempo` (fast) |
| R6 | accelerate | duration > 1.5× target, > 2 players | `eliminate_player` → least active |
| R7 | accelerate | duration > 2× target **and accelerate strictly dominant** (a tie never ends a game) | `end_game` |
| R8 | chaos | movement variance ≈ 0 ("statue") — dormant until producers ship the variance metric | `send_controller_effect` |
| R9 | chaos | periodic random nudge (injectable rng) | `send_controller_effect` |

**LLM-only interventions (#800).** Five intervention types are fully plumbed
(flag schema, action-sink mapping, game-side handlers, weight table) but are
**deliberately not emitted by any rule**: `adjust_volume`, `revive_player`,
`adjust_global_sensitivity`, `adjust_global_difficulty`, and
`set_pacing_profile`. They require judgment the deterministic ruleset cannot
encode (who deserves a revive; when reshaping the whole session's difficulty
or pacing is socially right) and are reserved for the M4 LLM path. They remain
reachable today via the permission layer for manual/operator dispatch — they
are intentionally unreached by `rules_decide`, not dead code.

**Policy constraints** (`policy.*` flags):

- `battery_threshold` (20): players below it lose controller effects and
  difficulty raises; session-wide demand raises are blocked while *anyone* is
  low; `eliminate_player` of a low-battery player stays available only as the
  accelerate-dominant graceful exit.
- `movement_variance_window` (10s): ALL chaos candidates (variance-triggered
  statue nudges and the random R9 nudge alike) are suppressed for one window
  after any difficulty intervention — the variance baseline is invalid, and a
  random rumble right after a tempo change would muddy attribution of the
  difficulty intervention's effect.
- `max_interventions_per_minute` (2): a **weighted** sliding-window budget per
  the [#722 research §5](../../docs/research/722-intervention-surface.md):
  soft 0.5 (audio cue, controller effect, volume), medium 1 (tempo, player
  sensitivity, shield), hard 2 (global sensitivity, eliminate, revive,
  end_game). **Enforced by the decision loop's permission layer, not the
  engine** (the reconciled #727/#728 stack unified #726's and #728's limiters
  into one — `decision/ratelimit.go`). Because the loop sees the allow-list and
  battery gates, the budget is charged only on decisions that pass those and
  fit — an improvement over #726's original "charge every emitted decision"
  (the engine could not see permissions). The engine now only caps emission at
  2 decisions per evaluation; `cd.cost` survives as a deterministic
  cheaper-first tie-break.

**Configuration seam (#726/#727/#731):** objectives, policy, and fitness
thresholds come from the `ObjectivesSource` / `PolicySource` / `FitnessSource`
interfaces (`decision/config.go`). #727 wires the **objectives** source to
OpenFeature: the loop publishes the per-cycle `objectives` flag into a
`LiveObjectives` source (`decision/objectives.go`) that the engine reads each
evaluation. #731 wires the **fitness** source the same way: the loop publishes
the per-cycle `fitness.*` flags into a `LiveFitness` source
(`decision/fitnesssource.go`), so threshold changes take effect on the next
cycle with no restart. Only **policy** still runs on `DefaultStaticConfig()`
(the flagd-schema defaults); objectives fall back to `{endurance: 1.0}` whenever
the flag resolves nothing.

## Fitness functions (#731)

Fitness functions turn "is this session *succeeding* for objective X?" into
observable, runtime-tunable numbers. The thresholds come entirely from the
`fitness.*` flags (never code), are evaluated **every cycle** against the live
`GameContext`, and both (a) **steer action selection** and (b) ride onto the
decision span as `fitness.evaluated`.

Three objectives have a fitness function; **chaos has none** — chaos is
*unpredictability* by definition ([#722 research
§4](../../docs/research/722-intervention-surface.md)), so there is no
success/degradation target to measure. It contributes no fitness value and no
selection pressure.

Each function produces a normalized `progress` in `0..1` (1 = satisfied, 0 =
maximally failing) and a `pressure = 1 − progress`. Functions whose required
signals are missing are **skipped** — they emit no value rather than fabricating
one.

| Objective | Flag (threshold) | Signal | Progress |
|-----------|------------------|--------|----------|
| `endurance` | `fitness.endurance.min_session_seconds` (120) | `session.duration_seconds` | `duration / min`, clamped to 1 — long sessions win |
| `balanced` | `fitness.balanced.max_skill_gap` (0.4) | spread (max−min) of per-player `skill_level` (≥ 2 known) | `1 − gap/(2·max)` — the 0.5 point is exactly at the threshold |
| `balanced` | `fitness.balanced.spike_survival_threshold` (0.8) | derived (see below) | `survival_ratio / threshold`, clamped to 1 |
| `accelerate` | `fitness.accelerate.target_session_seconds` (60) | `session.duration_seconds` | 1 up to the target, then `1 − overshoot/target` — overshoot = failing |

**Balanced is two sub-checks** (skill gap AND spike survival). The result's
`progress` is the *worse* of the two computable sub-checks and it is satisfied
only when every computed sub-check passes; the result is emitted when at least
one sub-check is computable.

**Spike-survival derivation.** There is no direct "survived a spike" signal, so
it is derived from the available per-player signals: a player **survives** when
their `movement_variance ≤ movement_intensity` — erratic swings (variance) that
exceed sustained effort (intensity) indicate a player thrown by spikes. The
`survival_ratio` is `survivors / active players with both signals`; the session
passes when `ratio ≥ spike_survival_threshold`. When no active player has both
signals the sub-check is skipped (the gap is honest, not fabricated).

**How results steer decisions.** In `scoreAndSort`, each candidate's score is
`urgency × weight[objective] × (1 + pressure[objective])`. A satisfied,
unevaluated, or fitness-less (chaos) objective contributes `pressure = 0` (no
change); a maximally failing one contributes `pressure = 1` (up to double the
effective urgency). So a failing endurance fitness amplifies the
endurance-serving candidates and can **flip the selected winner between
objectives** — without ever blocking a candidate outright. The cycle's full
evaluation is retained on the engine (`LastFitness()`) and read back by the loop
into `LayerState.FitnessEvaluated`.

**Runtime tunability.** Because the thresholds are evaluated and published every
cycle, changing a `fitness.*` flag mid-session changes the **next** cycle's
evaluation and the resulting action selection, with no restart (covered by
`TestLoop_MidSessionFlagChangeChangesOutcome`).

## Span schema (#724) — the trace is the audit log

Every evaluation that produces decisions emits one trace (hierarchy always
parent → child):

```
agent.span_received          one per triggering OTLP Export (backdated to arrival)
  └─ agent.decision          one per Decision the rules engine returns
       └─ agent.action       one per decision, wrapping the ActionSink call
```

**Traces are emitted only when the rules engine returns ≥ 1 decision** —
including decisions that end up *blocked*. Idle evaluations cost no spans.
Blocked actions are recorded (`decision.blocked = true` on both the decision
and action spans, ActionSink **not** called), never silently dropped.

### Decision-span attributes (#724 + #729)

Every `agent.decision` span always carries the **full** schema. Issue #729 lifts
the cycle's entire `LayerState` (every flag evaluated this cycle) onto the span
verbatim, so a single Jaeger trace answers: **which flags were in effect, which
backend decided, which objective was served, why this action, and was it
permitted.** Subsystems that do not exist yet contribute explicit placeholders so
the trace shows its complete shape from day one.

**Cycle-level flag attribution** (lifted from `LayerState`, same on every
decision in the cycle):

| Attribute | Source | Today | Real value arrives with |
|-----------|--------|-------|-------------------------|
| `agent.enabled` | existence flag `enabled` | live bool | — |
| `agent.mode` | existence flag `mode` | `"rules"` | LLM backend (M4) |
| `agent.objectives` | objective flag `objectives` | sorted `k=v` weights / `"unset"` | — |
| `agent.model` | capability flag `model` | live (e.g. `"phi4-mini"`) | consumed by M4 |
| `agent.prompt_variant` | capability flag `prompt_variant` | live | consumed by M4 |
| `interventions.allowed` | permission flag `interventions_allowed` | `"none"` / `a,b,c` | — |
| `policy.battery_threshold` | policy flag | live int | — |
| `policy.movement_variance_window` | policy flag | live int | — |
| `policy.max_interventions_per_minute` | policy flag | live int | — |
| `inference.configured` | the `model` flag value | live (e.g. `"phi4-mini"`) | — |
| `inference.used` | the engine that ran | `"rules"` | `"llm"` once M4 lands |
| `inference.fallback_reason` | why `llm` fell back | `"no_backend_available"` when `mode=llm`, else `""` | #741 `resolve_backend()` supplies the real reason / empties |
| `fitness.evaluated` (cycle) | `LayerState.FitnessEvaluated` — dotted per-objective thresholds + results (#731) | live (e.g. `endurance.session_progress=0.5`) | — |
| `gen_ai.agent.name` | agent identity | `"joustmania-agent"` | — |

**Per-decision attribution** (one decision per `agent.decision` span):

| Attribute | Today | Real value arrives with |
|-----------|-------|-------------------------|
| `decision.action` / `decision.reason` | real (rules/probe) | — |
| `decision.objective_served` | the objective the rule served / `"unset"` | — |
| `decision.blocked` | from the permission chain | — |
| `decision.block_reason` | `not_allowed` / `battery_threshold` / `rate_limit` (only when blocked) | — |
| `fitness.evaluated` (per-decision fallback) | the cycle-level evaluation (above) is authoritative; only Probe/Noop engines fall back to the rule's own `Decision.Fitness` | — |

The attribution attaches where **both** the rules and (future M4) LLM paths
converge (`decisionAttributes`, fed by the shared `LayerState`), so it is
path-agnostic — it works for the LLM path automatically once M4 lands.

#### Kill-switch trace (`agent.disabled`)

When the existence layer reports the agent **off** (`enabled=false`) the loop
short-circuits before any rules run, but still emits a **throttled** kill-switch
trace so "agent off" is visible in Jaeger: a root `agent.span_received` with a
single `agent.disabled` child (no `agent.action` child — nothing was decided)
carrying the same cycle-level flag attribution above (`agent.enabled=false`, the
capability and permission flags that were in effect). Throttled to one per second
so a disabled agent under heavy signal load does not flood the trace backend.

#### LLM prompt capture (M4 spike, #739)

When `agent.mode = "llm"` the loop has no inference backend yet, so it **captures
the prompt it would send** and falls back to the rules engine. Because the
`agent.decision` spans are lazy (emitted only when a decision is produced), the
prompt is recorded on a **dedicated** span, `agent.llm.prompt`, that emits on
every llm cycle regardless of whether a decision was made — greppable in Jaeger
by name. It is **throttled** on the same `decision.throttle_seconds` slot as the
evaluate log and `agent.disabled` (default 1s), so a steady-state llm agent emits
at most one capture per interval.

The span carries the full prompt plus its attribution (single builder
`llmPromptAttributes`, schema-complete every emission):

| Attribute | Value |
|-----------|-------|
| `gen_ai.operation.name` | `"chat"` |
| `gen_ai.request.model` | the `model` capability flag |
| `gen_ai.output.type` | `"json"` |
| `agent.mode` | `"llm"` |
| `agent.prompt_variant` | resolved prompt variant |
| `agent.objectives` | sorted `k=v` weights |
| `interventions.allowed` | the allow-list summary |
| `llm.prompt.system` / `llm.prompt.user` | the **full** prompt text (uncapped) |
| `llm.prompt.bytes` | `len(system)+len(user)` |
| `inference.configured` | the `model` flag |
| `inference.used` | `"rules"` (the rules engine decided) |
| `inference.fallback_reason` | `"no_backend_available"` |

The companion log line `agent.llm.prompt_captured` carries only metadata
(`session_id`, `variant`, `model`, `bytes`, `fallback_reason`) — the prompt text
lives on the span alone. **View in Jaeger:** open `http://localhost:8080/jaeger/`,
service `agent`, operation `agent.llm.prompt`. To replay a captured prompt against
a real model, copy `llm.prompt.system` / `llm.prompt.user` into two files and run
`scripts/replay-prompt.sh`. See
[docs/research/739-prompt-capture.md](../../docs/research/739-prompt-capture.md)
for the response contract and the forward path (#741 backend, #742 auth).

#### Post-game retrospective capture (M4, #844)

When a game **ends** (the `GameActive` true→false transition fires the store's
`OnGameEnd` hook with a pre-reset snapshot), the `decision.RetroCoordinator`
builds the prompt the agent would send to an **offline analyst** asking for
calibration tweaks for the next game, and records it on a dedicated
**`agent.llm.retro`** span — **capture-first**, exactly like #739, no backend
called yet. It emits **exactly once per session** (dedup on `SessionID`) and is
structurally isolated from the decision loop: it never touches the gate, the loop,
or the rate limiter, so a retrospective **cannot consume the in-game intervention
budget**.

The span carries the full retro prompt plus attribution (single builder
`retroPromptAttributes`, schema-complete every emission):

| Attribute | Value |
|-----------|-------|
| `gen_ai.operation.name` | `"chat"` |
| `gen_ai.request.model` | the `model` capability flag |
| `gen_ai.output.type` | `"json"` |
| `agent.mode` | `"retro"` |
| `agent.objectives` | sorted `k=v` weights |
| `interventions.allowed` | the allow-list summary |
| `session.id` | the finished session's id |
| `llm.retro.system` / `llm.retro.user` | the **full** retro prompt text (uncapped) |
| `llm.retro.bytes` | `len(system)+len(user)` |
| `inference.configured` | the `model` flag |
| `inference.used` | **`"none"`** (divergence — see below) |
| `inference.fallback_reason` | `"no_backend_available"` |

**Divergence from the in-game capture:** `inference.used` is `"none"`, **not**
`"rules"`. The in-game path falls back to the rules engine, so `"rules"` is the
honest "what decided this cycle". A retrospective has **no** rules fallback —
nothing runs in place of the analyst at game end — so `"none"` is the honest
value. The companion log line `agent.llm.retro_captured` carries only metadata
(`session_id`, `model`, `bytes`, `fallback_reason`). The suggestion contract maps
to the **calibration surface** (#766: `global_difficulty_factor`,
`pacing_profile`, `threshold_table`, `objective_variant`), and `replay-prompt.sh`
replays an `agent.llm.retro` span identically (copy `llm.retro.system` /
`llm.retro.user`). See
[docs/research/844-post-game-retro.md](../../docs/research/844-post-game-retro.md).

#### `fitness.evaluated` (#731)

`LayerState.FitnessEvaluated` (`map[string]float64`) holds the cycle-level
fitness evaluation — every threshold **and** every computed result, under dotted
`objective.signal` keys. The loop fills it each cycle by reading the engine's
`LastFitness().Evaluated()` and the existing span wiring lifts it onto the
decision (and disabled) span as the `fitness.evaluated` attribute, rendered as a
sorted `k=v` string slice. When the cycle-level evaluation is present it is
**authoritative** for the attribute; only engines without a fitness function
(Probe/Noop) fall back to the per-decision `Decision.Fitness` view, so the
attribute is always present on the span.

The full key vocabulary recorded on the span:

```
endurance.min_session_seconds   endurance.session_seconds   endurance.session_progress
balanced.max_skill_gap          balanced.skill_gap          balanced.skill_gap_progress
balanced.spike_survival_threshold  balanced.spike_survival_ratio  balanced.spike_survival_progress
accelerate.target_session_seconds  accelerate.session_seconds  accelerate.session_progress
```

Keys for an objective whose signals were missing that cycle are simply absent
(the function was skipped, not fabricated). chaos has no fitness function and so
no keys.

### Intervention-effect feedback (#918) — the game-path measurement loop

Outbound attribution (above) records the agent's *reasoning*; it does not measure
whether an intervention **helped**. The infra path has a closed loop (Bluetooth
fitness → rollback); the game path had nothing. #918 closes the **measurement** half
(it is measurement only — it never reverts a bad intervention):

1. **At dispatch** (the applied-decision path in `runDecision`) the loop stamps a
   **baseline** of the game's fitness/objective signals — the same dotted-key
   quantities `fitness.evaluated` carries — and tags it with a dispatch-unique
   `decision.intervention_id` (also stamped on the `agent.decision` span, so a Jaeger
   query joins *what the agent did* to *what happened next*).
2. **After a follow-up window** (`DefaultEffectWindow` = 20 s, env-tunable via
   `AGENT_EFFECT_WINDOW_SECONDS`) a per-game sampler re-reads the **same** game's
   signals (through the same `ContextProvider` the #917 async path uses, so per-game
   isolation holds) and recomputes the **same** fitness functions with the
   dispatch-time thresholds.
3. It emits an **effect record** attributed to the intervention id:

   - **Span** `agent.intervention.effect`: one backdated root (to dispatch time) per
     dispatched intervention, parenting one child span per evaluated fitness signal.
     Attributes: `intervention.id`, `intervention.type`, `intervention.objective`,
     `intervention.signal`, `intervention.fitness_baseline`,
     `intervention.fitness_followup`, `intervention.fitness_delta`,
     `intervention.window_seconds`, and `intervention.effect_aborted=true` when the
     follow-up could not be measured (game ended/evicted before the window, or a
     signal disappeared).
   - **Metric** `agent_intervention_effect_delta` (`Float64Histogram`, unit `1`): the
     `follow_up − baseline` change, labeled `intervention.type`,
     `intervention.objective`, `intervention.signal`. No point is recorded for an
     aborted follow-up.

   The M5 intervention dashboard (#791/#792) plots the intervention → effect pairs, and
   the #844 retro prompt reads per-intervention effect evidence; both consume this
   span + metric.

**Lifecycle safety (#923 goleak):** each follow-up runs in a goroutine tracked on a
`WaitGroup` (joined by `AwaitInflight` at shutdown) and bounded by the agent root
context. It selects on the follow-up timer **and** `rootCtx.Done()`: on shutdown it
stops the timer and returns without emitting; the in-flight pending set is capped
(`maxPendingEffects`) so a burst of dispatches cannot launch unbounded goroutines.
Measurement is best-effort and never on the hot decision path.

### Semantic conventions

OTel semantic conventions ([semconv v1.34.0](https://pkg.go.dev/go.opentelemetry.io/otel/semconv/v1.34.0))
are used wherever one honestly applies:

| Where | Convention |
|-------|-----------|
| `agent.span_received` | `rpc.system=grpc`, `rpc.service` (OTLP `TraceService`/`MetricsService`), `rpc.method=Export`; span kind `SERVER` |
| `agent.decision` | `gen_ai.agent.name` identity (the full [GenAI agent conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-agent-spans/) apply once an LLM inference path exists — `gen_ai.provider.name` etc. land as a `gen_ai.*` child span in llm mode) |
| `agent.decision` event | `feature_flag` span event (`feature_flag.key=interventions.allowed`, `feature_flag.provider.name`, `feature_flag.result.variant`) — same shape the openfeature OTel hooks emit in the Python services; provider is `"stub"` until flagd (#725) |
| `agent.action` failures | `span.RecordError` + `error.type` + status `ERROR` |

`decision.*`, `fitness.*`, `agent.mode`, `agent.objectives` and
`interventions.allowed` have no semantic convention and are custom to this
project.

### Self-ingestion safety

The collector's `otlp/agent` exporter fans the **agent's own spans back to the
agent**. Two layers prevent a feedback loop:

1. Naturally: the agent's spans carry no recognized game signals, so extraction
   reports "nothing updated" and no evaluation (hence no new span) is triggered.
2. Defense-in-depth: the extractors skip any resource whose `service.name`
   equals the agent's own `OTEL_SERVICE_NAME`.

A collector-side filtered pipeline (excluding the agent from its own fan-out)
is a possible follow-up but not needed today.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_LISTEN_ADDR` | `:4317` | OTLP gRPC receiver listen address |
| `AGENT_HEALTH_ADDR` | `:13134` | HTTP health endpoint listen address (`GET /healthz`) |
| `FLAGD_HOST` | `flagd` | flagd host for OpenFeature flag evaluation |
| `FLAGD_PORT` | `8013` | flagd gRPC **evaluation** port (RPC resolver) |
| `LOG_LEVEL` | `info` | Log verbosity |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | set in compose | Self-telemetry export (decision audit traces → collector → Jaeger); no-op when unset |
| `OTEL_SERVICE_NAME` | `agent` | Service identity; also drives the self-ingestion skip |
| `AGENT_PROBE_DECISIONS` | _unset_ | `true` enables the demo/verification probe: a synthetic `noop` decision (and thus a full audit trace) at most every 5 s. Never for production sessions. See [Probe mode](#probe-mode-agent_probe_decisions) |
| `AGENT_INTERVENTIONS_ENABLED` | `false` | `true` swaps the no-op action sink for the real intervention **Writer** (#730). Default off keeps the scaffold inert |
| `INTERVENTIONS_FLAG_PATH` | `/etc/flagd/interventions.json` | Path of the flagd interventions file the Writer rewrites (must be the bind-mounted file flagd watches) |
| `AGENT_GAME_SUMMARY_DIR` | `/var/lib/joustmania/agent/summaries` | Directory the M7-1 game narrative builder (#928) writes one JSON game summary per game into (real **and** shadow), created if missing; atomic temp+rename so a reader never sees a partial file |
| `AGENT_EXPERIMENTS_ENABLED` | `false` | **Opt-in for the #991 experiment cohort loop** (epic #982). `true` constructs the `experiment.Registry` with the real seams (spawner/targeting/verdict/promoter) and runs the declare→spawn→conclude→verdict→(gated)promote loop. **Default off ⇒ no Registry, no shadow spawns, no targeting writes, no promotions — the agent behaves exactly as before.** Promotion still routes through the existing `code_improvement` gates + kill-switch |
| `AGENT_MAX_SHADOW_GAMES` | `20` | Fixed cap on concurrent shadow games across all live experiments (epic #982 decision 2); the registry round-robins this **capacity-bookkeeping** cap across experiments |
| `AGENT_SHADOW_EFFECTIVE_CONCURRENCY` | `1` | **Effective** in-flight shadow-spawn concurrency (#998), bounding how many concurrent starts the registry actually attempts. The game-coordinator runs **one game at a time** and rejects concurrent starts ("Game already in progress"); defaulting to 1 stops the registry over-spawning doomed starts. Raise it only when the coordinator is taught to run concurrent shadow games. Clamped to `AGENT_MAX_SHADOW_GAMES`. A coordinator-at-capacity rejection is treated as **backpressure** (release + retry next tick), not a spawn failure |
| `AGENT_EXPERIMENT_TICK_SECONDS` | `30` | Cadence of the registry's `AllocateAndSpawn` (refills free shadow capacity) |
| `AGENT_EXPERIMENT_DIR` | `/var/lib/joustmania/agent/experiments` | Durable experiment journal root (intent/events/summary per experiment); the registry rehydrates from it on startup |
| `AGENT_VERDICT_MIN_N` | `8` | #979 min games per arm before a conclusive verdict (else inconclusive) |
| `AGENT_VERDICT_EFFECT_THRESHOLD` | `0.5` | #979 minimum \|Cohen's d\| for a promote/discard verdict |
| `AGENT_EXPERIMENT_SEED_FLAG` | _unset_ | When set (and the loop is enabled), declares ONE env-seeded experiment at startup on this game.json flag — the simplest declaration trigger for the demo. Paired with `AGENT_EXPERIMENT_SEED_VALUE` / `_OBJECTIVE` / `_TARGET_N` / `_HYPOTHESIS` |
| `AGENT_EXPERIMENT_SEED_VALUE` | _unset_ | The seed experiment's experimental value. Parsed as a number when it parses as a float, else bool for `true`/`false`, else the raw string. Its JSON kind must match the flag's existing variants (the Gate's type guard rejects a mistyped seed at the targeting write) |
| `AGENT_EXPERIMENT_SEED_OBJECTIVE` | `balanced` | The seed experiment's fitness objective (`endurance` / `balanced` / `accelerate` / `chaos`) |
| `AGENT_EXPERIMENT_SEED_TARGET_N` | _unset_ | Target games per arm for the seed experiment (positive int; falls back to the registry default when unset) |
| `AGENT_EXPERIMENT_SEED_HYPOTHESIS` | _generated_ | Free-text hypothesis recorded on the seed experiment's intent |

> The Go agent uses the flagd **RPC** resolver (gRPC evaluation port `8013`),
> not the in-process sync port `8015` that the Python services use.

### Experiment cohort loop dry run (#982/#991/#999)

`make dry-run` brings the cohort loop up **runnable out-of-the-box** — latest
images, mock controllers, full observability, the loop enabled, and one seeded
experiment. See the [dry-run runbook](../../docs/agent-dry-run-runbook.md) for
the operator steps and the silent-failure traps it avoids (stale `IMAGE_TAG`,
the kill-switch default, and the agent↔flagd flag-dir mismatch under the ci dir).

The loop ACTS only when **three independent gates** are all satisfied — kept
separate so a dry run can spawn shadow games without ever risking a real-default
change:

1. **Experiments opt-in** — `AGENT_EXPERIMENTS_ENABLED=true`. Builds the
   `experiment.Registry` and runs declare → spawn → conclude → verdict →
   (gated) promote. Default `false` ⇒ no Registry, no shadow spawns, no
   targeting writes, no promotions.
2. **Master kill-switch** — `agent.json` `enabled` = `on` (read live each
   cycle; `off` is the fail-closed default). When `off` the loop short-circuits
   with a throttled `kill-switch: agent disabled` span. Flip the **dry-run** copy
   with `./scripts/agent-killswitch.sh on` — production default stays `off`.
3. **`code_improvement.*` promotion gates** — separate flags that gate the
   **real-default promotion** action only (still subject to the invariant gate +
   kill-switch). They stay at defaults in the dry run, so a concluded experiment
   yields a **verdict** but does **not** rewrite the real game.json default.

### Probe mode (`AGENT_PROBE_DECISIONS`)

`AGENT_PROBE_DECISIONS=true` swaps the rules engine for `ProbeRules`, which emits
one synthetic `noop` decision at most every 5 s. Its only purpose is to drive a
**full audit trace** (`agent.span_received → agent.decision → agent.action`)
without waiting for a live game to trigger the real rules — invaluable for
verifying the trace pipeline end to end.

**With the default `interventions_allowed` variant, probe decisions are blocked
by design.** `noop` is not in the `ambient`/`standard`/`full` allow-lists, so the
permission layer blocks every probe decision with `decision.blocked=true`,
`decision.block_reason=not_allowed`. This is still useful: the
`agent.span_received → agent.decision` spans are emitted (a blocked decision is
traced, not dropped), so the OBSERVE→DECIDE path and the span schema are fully
exercised — only the `agent.action` dispatch is withheld.

**To make probe decisions dispatch** (exercise the *whole* path including
`agent.action`), flip `interventions_allowed` to the dedicated **`probe`**
variant (`["noop"]`) in [`services/flagd/agent.json`](../flagd/agent.json):

```json
"interventions_allowed": { "defaultVariant": "probe" }
```

`noop` is a **harmless no-op in the action sink**: the `Writer` recognizes it and
returns success **without writing any flag** (`actions/writer.go`,
`InterventionNoop`). So with the `probe` variant + `AGENT_INTERVENTIONS_ENABLED=true`
+ `enabled=on`, a probe decision flows through allow-list, battery, and rate-limit
gates, reaches the real `Writer`, and completes the `agent.action` span — all
without touching the game. Revert by flipping `interventions_allowed` back to
`ambient` (or `none`).

### Lifecycle flags — hot-reloaded via OpenFeature config-change (#766 F5, #927)

The agent's lifecycle and throttle calibration values live in the flagd `agent`
domain ([`services/flagd/agent.json`](../flagd/agent.json)). Like the four
decision-cycle flag layers above they are **hot-reloadable** — but via a
different mechanism. #766 F5 originally read them once at startup, so retuning
the store TTLs / eviction cadence / throttle needed a restart. **#927 makes them
live** by wiring the **OpenFeature provider's configuration-change event
listener** (`openfeature.ProviderConfigChange`, the mechanism the maintainer
requested): flagd emits a config-change when its source changes, the Go SDK
surfaces the event, and a domain-scoped handler re-evaluates these four flags
into a shared, concurrency-safe `flags.LifecycleHolder`
([`flags/holder.go`](flags/holder.go)). The consumers read the holder on their
hot path — the GameContext store reads the TTLs at eviction time, `main.go`'s
eviction ticker re-reads (and `Reset`s to) the interval each loop, and the
decision loop reads the throttle in `shouldLog()` — so **changing one takes
effect with no restart**, and **without** polling the flag client per decision
cycle (a holder read is a lock-free atomic load; only the rare config-change
event re-reads flagd). The holder is **primed once at startup**, so values are
correct before any event arrives; if flagd is not yet reachable each value falls
back to its safe default, which reproduces the former hardcoded constant exactly.

| Flag | Default | Configures |
|------|---------|------------|
| `lifecycle.player_ttl_seconds` | `5` | How long a silent player is retained before eviction |
| `lifecycle.session_grace_seconds` | `15` | How long an ended session lingers before its session-scoped state resets |
| `lifecycle.evict_interval_seconds` | `1` | How often the eviction ticker fires |
| `decision.throttle_seconds` | `1` | How often the `agent.evaluate` log line and the `agent.disabled` span are emitted |

A non-positive value for any of these falls back to its default (a zero TTL or
ticker interval would be unsafe), both at startup and on every hot-reload, so a
transient bad flag read during a config-change can never collapse a TTL.

## Action sink (#730) — applying decisions as flag writes

The agent **never calls the game services over gRPC**. It applies a decision by
**rewriting the flagd `interventions` flag file**
(`services/flagd/interventions.json`); flagd's file-watch fires
`PROVIDER_CONFIGURATION_CHANGED` in <100 ms, the game coordinator re-evaluates
the intervention flags and converges on their contents
(see `docs/research/722-intervention-surface.md` §8). The agent is the **sole
writer** of this file; the `Writer` serializes its own dispatches with a mutex.

### Transport / write semantics

- **Read-modify-write IN PLACE.** The file is truncated and rewritten at the same
  fd — **no temp+rename**, because `rename(2)` over a docker bind mount that flagd
  is inotify-watching fails with `EBUSY`. This mirrors the proven admin-mode
  pattern in `lib/flag_config_writer.py`.
- **Byte-stable.** Only the flags being mutated change; every untouched flag
  round-trips byte-for-byte (order-preserving document model). Output is
  `indent=2` + trailing newline — identical formatting to admin-mode writes.

### Flag shapes (game-side reader contract)

- **Edge-triggered one-shots** (`audio_cue`, `controller_effect`,
  `eliminate_player`, `revive_player`, `end_game`): the dedicated `active`
  variant is overwritten with `"<nonce>:<payload>"` (`end_game` is nonce-only)
  and `defaultVariant` flips to it. A **fresh unique nonce per dispatch**
  (monotonic counter + random suffix) makes the reader apply exactly once on
  nonce change. Payloads: eliminate/revive = `<serial>`; `audio_cue` =
  `<sound_id>`; `controller_effect` = `<serial>:<effect>` (empty serial =
  broadcast).
- **Session state-shaped** (`music_tempo_override`, `volume_override`,
  `global_sensitivity_override`): the `active` variant is set to the typed value
  and `defaultVariant` flips to it; reverting flips `defaultVariant` back to the
  neutral variant (`none`).
- **Per-player state-shaped** (`player_sensitivity_factor`, `shield_seconds`):
  written via a flagd **targeting** JsonLogic if-ladder keyed on
  `targetingKey == serial`. Each driven serial gets an `agent_<serial>` variant;
  unmatched serials fall through to `defaultVariant`. Removing a player drops its
  branch and variant; the last removal drops the targeting block entirely.

### Decision value contract

The rules engine (#726) leaves `Decision.Value` empty, so the Writer supplies
per-type defaults: audio cue `agent_cue`, controller effect `rumble`, music tempo
`1.15`, volume `0.7`, global sensitivity `2`, player sensitivity `1.5`, shield
`5`. An explicit `Decision.Value` (sound id / effect name / numeric target as a
decimal string) overrides the default.

| Property | Value |
|----------|-------|
| **OTLP port** | 4317 |
| **Health port** | 13134 |
| **Health check** | `GET /healthz` |

## Shadow-game runner (#778)

The `gamerunner/` package launches **mock-only "shadow" games** on the live
stack: games fielded entirely with RESERVED mock controllers (#777), so they
never appear in the menu lobby and never collide with a real, menu-driven game.
Unlike the action sink (#730), which acts through flagd file writes, game-start
has no flag representation — it is a **direct gRPC** capability, and these are
the agent's first gRPC clients toward the game stack.

`RunShadowGame(ctx, spec)`:

1. `AddControllers(count=N, reserved=true, tag="agent:<runID>")` → serials
   (menu-invisible synthetic players).
2. `StreamGameEvents(StartGameConfig{...})` — headless start (no lobby); the
   coordinator-assigned `game_id` is captured from the first stamped event
   (`GameEvent.game_id`, #776).
3. Drives the game: periodic `SimulateMovement` for liveliness + paced
   `SimulateDeath` per player (paced above the mock's ~1s death-hold) to reach
   the last-player-standing condition.
4. Awaits a terminal event (`game_ended` / `game_force_ended` / `game_error`)
   with a timeout; on timeout calls `ForceEndGame{game_id}`.
5. ALWAYS removes its reserved controllers (deferred), so a failed run never
   orphans controllers.

`SweepOrphans(ctx, "agent:")` is the crash backstop: it removes reserved
controllers whose tag has the prefix but which no live game (`ListGames`)
references. The env trigger sweeps before each run.

A run gets an `agent.shadow_game.run` span with `run_id`, `mode`, `game_id`,
`outcome`, `terminal_event`, and `events_seen` attributes.

### Trigger (env, one-shot)

The trigger is intentionally minimal — one env-gated path, mirroring
`AGENT_INTERVENTIONS_ENABLED` / `AGENT_ROLLOUT_ENABLED`. When
`AGENT_SHADOW_GAME=true`, the agent sweeps orphans then runs ONE shadow game at
startup, then continues its normal observe loop.

| Env var | Default | Purpose |
|---------|---------|---------|
| `AGENT_SHADOW_GAME` | `false` | Enable the one-shot shadow game |
| `SHADOW_GAME_MODE` | `JoustFFA` | Game mode |
| `SHADOW_GAME_PLAYERS` | `4` | Reserved controller count (≥2) |
| `SHADOW_GAME_SENSITIVITY` | `2` | Death sensitivity 0-4 |
| `SHADOW_GAME_TIMEOUT_SECONDS` | `90` | Force-end deadline |
| `GAME_COORDINATOR_ADDR` | `game-coordinator:50053` | Coordinator gRPC |
| `MOCK_CONTROLLER_ADDR` | `controller-manager:50062` | Mock control gRPC |

The Go stubs for the two services live committed under `gen/` (generated by
`make protos-agent` — self-contained so `go test` and the Dockerfile build with
no buf step).

## Files

```
services/agent/
├── Dockerfile      # Two-stage: cross-compiled static Go build, alpine runtime
├── main.go         # Wiring: config, OTLP receiver, health server, lifecycle, shadow-game trigger
├── otel.go         # OTLP/self-telemetry setup
├── receiver.go     # OTLP span/metric ingestion + extraction into GameContext
├── gamecontext/    # GameContext accumulation, session identity, eviction
├── infracontext/   # InfraContext: controller.bluetooth_health observe path (#733)
├── gate/           # should_evaluate gating logic
├── flags/          # OpenFeature/flagd four-layer control flags (existence, objective, capability, permission)
├── actions/        # Action sink (#730): rewrites the flagd interventions file in place
├── decision/       # Decision loop + LayerState + rate limiter + rules engine (#726) + ActionSink interface
├── gamerunner/     # Shadow-game runner (#778): mock-only games driven via direct gRPC
├── gen/            # Committed grpc-go stubs for GameCoordinator + MockController (make protos-agent)
├── go.mod
└── go.sum
```

## Running tests

```bash
go test -race ./...
```

## Local smoke test

With the compose stack up (Agent reachable as `agent:4317` on the `joustmania`
network), push synthetic metrics with
[telemetrygen](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/cmd/telemetrygen):

```bash
docker run --rm --network joustmania \
  ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:latest \
  metrics --otlp-insecure --otlp-endpoint agent:4317 --duration 5s
```

## See Also

- [Intervention Surface research](../../docs/research/722-intervention-surface.md)
- [Ownership Model](../../docs/OWNERSHIP_MODEL.md) -- human/agent settings arbitration & composition rules (#814/#820)
- [OTel Collector config](../otel-collector/) -- defines the `otlp/agent` exporter
- [Architecture](../../docs/ARCHITECTURE.md)
