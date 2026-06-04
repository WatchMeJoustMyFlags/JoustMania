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
opens it invokes the decision hooks:

- **Rules engine** (#726) — `ObjectiveRules`, the objective-weighted decision
  logic below. Active by default.
- **Action sink** (#730) — applies intents via OpenFeature/flagd. **Still a
  no-op**: decisions are traced, nothing is applied yet.

See [`docs/research/722-intervention-surface.md`](../../docs/research/722-intervention-surface.md)
for the intervention-surface design.

## Rules engine (#726)

`ObjectiveRules` is the `rules_decide(context, objectives)` path — the non-LLM
intelligence and the final link of the inference fallback chain. Each rule
yields candidates with an urgency (0–1) and the objective they serve; the
final score is `urgency × weight[objective]`. Candidates below a minimum score
(0.10) are dropped, the rest are admitted best-first (ties: cheaper first, then
name) while the weighted budget allows — at most 2 decisions per evaluation,
and the rule set runs at most once per second.

| Rule | Objective | Trigger | Intervention |
|------|-----------|---------|--------------|
| R1/R2 | endurance | session younger than `fitness.endurance.min_session_seconds` while players are eliminated | `adjust_music_tempo` (slow) / `play_audio_cue` |
| R3 | balanced | skill spread > `fitness.balanced.max_skill_gap` | `adjust_player_sensitivity` → highest-skill outlier |
| R4 | balanced | weakest player while the field shrinks | `grant_shield` → weakest |
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
- `movement_variance_window` (10s): variance/chaos candidates are suppressed
  for one window after any difficulty intervention (the baseline is invalid —
  players adapt).
- `max_interventions_per_minute` (2): a **weighted** sliding-window budget per
  the [#722 research §5](../../docs/research/722-intervention-surface.md):
  soft 0.5 (audio cue, controller effect, volume), medium 1 (tempo, player
  sensitivity, shield), hard 2 (global sensitivity, eliminate, revive,
  end_game). The engine charges every decision it **emits**, including ones
  the permission layer later blocks — it cannot see permissions (layering),
  and the game coordinator's flag-application layer (#730) is the
  authoritative backstop.

**Configuration seam (#727):** objectives, policy, and fitness thresholds come
from the `ObjectivesSource` / `PolicySource` / `FitnessSource` interfaces
(`decision/config.go`). Until #727 wires OpenFeature, `DefaultStaticConfig()`
supplies the flagd-schema defaults with objectives `{endurance: 1.0}` (the
flag's own `balanced_focused` default surfaces once the flag is actually read).

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
| `LOG_LEVEL` | `info` | Log verbosity |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | set in compose | Self-telemetry export (decision audit traces → collector → Jaeger); no-op when unset |
| `OTEL_SERVICE_NAME` | `agent` | Service identity; also drives the self-ingestion skip |
| `AGENT_PROBE_DECISIONS` | _unset_ | `true` enables the demo/verification probe: a synthetic `noop` decision (and thus a full audit trace) at most every 5 s. Never for production sessions |

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
├── decision/       # Stub decision hooks (rules engine #726, action sink #730)
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
