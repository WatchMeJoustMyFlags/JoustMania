# Dynatrace Monaco Configuration

This directory contains [Monaco](https://github.com/Dynatrace/dynatrace-configuration-as-code) configuration-as-code for deploying JoustMania dashboards and alert rules to Dynatrace.

## Prerequisites

1. **Monaco CLI** installed ([installation guide](https://www.dynatrace.com/support/help/manage/configuration-as-code/monaco/installation))
2. **Dynatrace environment** with an API token that has these scopes:
   - `document:documents:write`
   - `document:documents:read`
   - `settings.write` (for metric events)
   - `settings.read`

## Environment Variables

```bash
export DYNATRACE_ENVIRONMENT_URL=https://{your-env-id}.apps.dynatrace.com
export DYNATRACE_API_TOKEN=dt0c01.XXXXXXXX.YYYYYYYY
```

## Deploying

```bash
# Dry run (validate without applying)
monaco deploy manifest.yaml --dry-run

# Deploy dashboards and alerts
monaco deploy manifest.yaml
```

## Structure

```
services/dynatrace/
├── manifest.yaml                           # Monaco manifest
├── README.md                               # This file
└── projects/
    └── joustmania/
        ├── dashboards/                     # Dynatrace dashboard documents
        │   ├── service-health-overview.yaml
        │   ├── service-health-overview.json
        │   ├── system-overview.yaml
        │   ├── system-overview.json
        │   ├── game-quality.yaml
        │   ├── game-quality.json
        │   ├── controller-overview.yaml
        │   ├── controller-overview.json
        │   ├── game-analytics.yaml
        │   └── game-analytics.json
        └── metric-events/                  # Dynatrace metric event alerts
            ├── alerts.yaml
            └── alert-templates/
                ├── controller-low-battery.json
                ├── controller-disconnected.json
                ├── high-input-latency.json
                ├── high-cpu-usage.json
                ├── high-memory-usage.json
                ├── low-game-loop-hz.json
                ├── high-grpc-latency.json
                ├── service-down.json
                ├── poor-frame-consistency.json
                ├── high-gc-pauses.json
                ├── game-crash-rate.json
                ├── service-update-pending.json
                └── high-grpc-client-error-rate.json
```

## Dashboards

| Dashboard | Description | Grafana Equivalent |
|-----------|-------------|-------------------|
| Service Health Overview | Service up/down, CPU, memory, gRPC metrics | `service-health-overview.json` |
| System Overview | Combined system status and performance | `system-overview.json` |
| Game Quality | Frame timing, jitter, game loop rate | `game-quality.json` |
| Controller Overview | Battery, signal, connection status | `controller-overview.json` |
| Game Analytics | Movement patterns, near-death events | `game-analytics.json` |

### Dashboards not converted

- **Realtime Acceleration** — uses Grafana Live WebSocket streaming, no Dynatrace equivalent
- **Metrics Pipeline Comparison** — internal tooling for VictoriaMetrics vs Prometheus
- **Feature Flags** — flagd-specific, low value for Dynatrace

## Alert Rules

All 13 Prometheus alert rules from `services/prometheus/alerts.yml` are converted to Dynatrace metric events. See `metric-events/alerts.yaml` for the full list.

### Conversion notes

- **Histogram percentiles**: Dynatrace uses native percentile aggregation (P90) instead of PromQL's `histogram_quantile()` with bucket math.
- **Game crash rate**: Simplified from a ratio of two counters (`force_ended / started`) to a direct threshold on `games_force_ended_total` delta, since Dynatrace metric events don't support cross-metric ratios.
- **Sampling windows**: Prometheus `for: 2m` maps to approximately `violatingSamples: 5, samples: 10` at 30-second intervals.
