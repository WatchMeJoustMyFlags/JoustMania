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
  **Still a no-op**: decisions are traced, nothing is applied yet.

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

**Configuration seam (#726/#727):** objectives, policy, and fitness thresholds
come from the `ObjectivesSource` / `PolicySource` / `FitnessSource` interfaces
(`decision/config.go`). #727 wires the **objectives** source to OpenFeature: the
loop publishes the per-cycle `objectives` flag into a `LiveObjectives` source
(`decision/objectives.go`) that the engine reads each evaluation, so objective
changes take effect with no restart. Policy and fitness still run on
`DefaultStaticConfig()` (the flagd-schema defaults) and fall back to objectives
`{endurance: 1.0}` whenever the flag resolves nothing.

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

### Decision-span attributes

Every `agent.decision` span always carries the **full** schema; subsystems that
do not exist yet contribute explicit placeholders so the trace shows its
complete shape from day one:

| Attribute | Today | Real value arrives with |
|-----------|-------|-------------------------|
| `agent.mode` | `"rules"` | LLM backend issue |
| `agent.objectives` | `"unset"` | #725 / #731 |
| `interventions.allowed` | `"unrestricted"` | #725 (flagd) |
| `inference.configured` / `inference.used` | `"none"` | LLM backend |
| `inference.fallback_reason` | `""` | LLM backend |
| `decision.action` / `decision.reason` | real (rules/probe) | — |
| `decision.objective_served` | `"unset"` | #731 |
| `decision.blocked` | from `Permissions.Allowed()` | #725 |
| `fitness.evaluated` | `[]` | #731 |
| `gen_ai.agent.name` | `"joustmania-agent"` | — |

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
| `AGENT_PROBE_DECISIONS` | _unset_ | `true` enables the demo/verification probe: a synthetic `noop` decision (and thus a full audit trace) at most every 5 s. Never for production sessions |
| `AGENT_INTERVENTIONS_ENABLED` | `false` | `true` swaps the no-op action sink for the real intervention **Writer** (#730). Default off keeps the scaffold inert |
| `INTERVENTIONS_FLAG_PATH` | `/etc/flagd/interventions.json` | Path of the flagd interventions file the Writer rewrites (must be the bind-mounted file flagd watches) |

> The Go agent uses the flagd **RPC** resolver (gRPC evaluation port `8013`),
> not the in-process sync port `8015` that the Python services use.

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

## Files

```
services/agent/
├── Dockerfile      # Two-stage: cross-compiled static Go build, alpine runtime
├── main.go         # Wiring: config, OTLP receiver, health server, lifecycle
├── otel.go         # OTLP/self-telemetry setup
├── receiver.go     # OTLP span/metric ingestion + extraction into GameContext
├── gamecontext/    # GameContext accumulation, session identity, eviction
├── gate/           # should_evaluate gating logic
├── flags/          # OpenFeature/flagd four-layer control flags (existence, objective, capability, permission)
├── actions/        # Action sink (#730): rewrites the flagd interventions file in place
├── decision/       # Decision loop + LayerState + rate limiter + rules engine (#726) + ActionSink interface
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
- [OTel Collector config](../otel-collector/) -- defines the `otlp/agent` exporter
- [Architecture](../../docs/ARCHITECTURE.md)
