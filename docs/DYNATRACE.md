# Dynatrace Integration

JoustMania supports optional parallel telemetry export to [Dynatrace](https://www.dynatrace.com/) alongside the local observability stack (Jaeger, VictoriaMetrics, Grafana, Loki).

## Architecture

```
                    ┌──────────────────┐
                    │  OTEL Collector  │
                    │  (unified hub)   │
                    └────────┬─────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
              ▼              ▼              ▼
    ┌─────────────┐  ┌────────────┐  ┌───────────┐
    │ Local Stack │  │ Dynatrace  │  │  (future)  │
    │             │  │  Cloud     │  │            │
    │ - Jaeger    │  │            │  │            │
    │ - VicMetrics│  │ - Traces   │  │            │
    │ - Prometheus│  │ - Metrics  │  │            │
    │ - Loki      │  │ - Logs     │  │            │
    │ - Grafana   │  │            │  │            │
    └─────────────┘  └────────────┘  └───────────┘
```

The Dynatrace integration adds **4 parallel pipelines** to the OTEL Collector that export traces, metrics (OTLP + scraped infra), and logs to Dynatrace via OTLP HTTP. The local stack is completely unaffected.

### Key design decisions

- **Delta temporality**: Dynatrace requires delta counters. The `cumulativetodelta` processor converts cumulative counters only in Dynatrace pipelines, keeping local pipelines on cumulative.
- **Batch sizing**: Dynatrace pipelines use `batch` (10s timeout) instead of `batch/fast` (100ms) since Dynatrace has 1-minute metric resolution.
- **Memory**: The collector memory limit is bumped from 256MB to 384MB to accommodate the extra pipelines.

## Prerequisites

1. A Dynatrace environment (SaaS or Managed)
2. An API token with these scopes:
   - `metrics.ingest` — push metrics via OTLP
   - `traces.ingest` — push traces via OTLP
   - `logs.ingest` — push logs via OTLP

## Quick Start

### 1. Configure credentials

Copy the example env file and fill in your values:

```bash
cp .env.dynatrace.example .env
# Edit .env with your Dynatrace endpoint and API token
```

Or export directly:

```bash
export DYNATRACE_ENDPOINT=https://{your-env-id}.live.dynatrace.com/api/v2/otlp
export DYNATRACE_API_TOKEN=dt0c01.XXXXXXXX.YYYYYYYY
```

### 2. Start with Dynatrace export

```bash
make up-dynatrace
```

This is equivalent to:

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.dynatrace.yml up -d
```

### 3. Verify in Dynatrace

- **Traces**: Go to Distributed Traces and filter by service name
- **Metrics**: Go to Metrics and search for `game_`, `controller_`, `grpc_`
- **Logs**: Go to Logs & Events

## Deploying Dashboards and Alerts

JoustMania includes pre-built Dynatrace dashboards and alert rules managed via [Monaco](https://github.com/Dynatrace/dynatrace-configuration-as-code) (Dynatrace configuration-as-code).

### Prerequisites

- [Monaco CLI](https://www.dynatrace.com/support/help/manage/configuration-as-code/monaco/installation) installed
- API token with `document:documents:write`, `document:documents:read`, `settings.write`, `settings.read` scopes

### Deploy

```bash
# Set environment URL (note: .apps. not .live.)
export DYNATRACE_ENVIRONMENT_URL=https://{your-env-id}.apps.dynatrace.com

# Dry run
cd services/dynatrace
monaco deploy manifest.yaml --dry-run

# Deploy
monaco deploy manifest.yaml
```

### Included dashboards

| Dashboard | Description |
|-----------|-------------|
| Service Health Overview | Service up/down status, CPU, memory, gRPC metrics |
| System Overview | Combined system status and performance |
| Game Quality | Frame timing, jitter, game loop rate, dropped frames |
| Controller Overview | Battery levels, signal strength, connection status |
| Game Analytics | Movement patterns, near-death events, thresholds |

### Included alerts (13 rules)

All alerts from the local Prometheus stack are converted to Dynatrace metric events:

| Alert | Condition | Severity |
|-------|-----------|----------|
| ControllerLowBattery | Battery < 2/5 | Warning |
| ControllerDisconnected | Connection lost | Critical |
| HighInputLatency | P90 input lag > 50ms | Warning |
| HighCPUUsage | CPU > 80% | Warning |
| HighMemoryUsage | Memory > 500MB | Warning |
| LowGameLoopHz | Loop rate < 55Hz | Warning |
| HighGRPCLatency | P90 gRPC latency > 100ms | Warning |
| ServiceDown | Service unreachable | Critical |
| PoorFrameConsistency | Consistency < 95% | Warning |
| HighGCPauses | P90 GC pause > 10ms | Warning |
| GameCrashRate | Force-ended games detected | Critical |
| ServiceUpdatePending | Config update available | Info |
| HighGRPCClientErrorRate | Client errors > 30/min | Warning |

## Known Limitations

- **1-minute metric resolution**: Dynatrace ingests metrics at 1-minute granularity. The local stack (VictoriaMetrics) retains sub-second resolution via `batch/fast`.
- **No real-time acceleration**: The Grafana Live WebSocket streaming dashboard has no Dynatrace equivalent.
- **Histogram percentiles**: Dynatrace uses P90 natively instead of arbitrary percentile computation from histogram buckets. P95/P99 values may differ slightly.
- **Game crash rate alert**: Simplified from a ratio of two counters to a direct threshold, since Dynatrace metric events don't support cross-metric math.

## Disabling Dynatrace Export

Simply stop using the Dynatrace compose override:

```bash
docker compose up -d  # no -f docker-compose.dynatrace.yml
```

No configuration changes are needed — the local stack continues to work identically.
