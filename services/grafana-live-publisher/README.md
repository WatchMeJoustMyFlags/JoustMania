# Grafana Live Publisher

Bridges OTLP metrics into Grafana Live for real-time WebSocket streaming to dashboards.

## Overview

The Grafana Live Publisher is a lightweight Go service that receives OTLP metrics (JSON format) from the OpenTelemetry Collector, filters for controller acceleration data, and pushes it to Grafana Live using the InfluxDB line protocol push API. This enables sub-second acceleration waveform updates in Grafana dashboard panels via WebSocket, without polling.

## How It Works

1. OTel Collector exports acceleration metrics to this service's `/v1/metrics` endpoint
2. The service parses the OTLP JSON payload and extracts `controller_accel_*` gauge metrics
3. Metrics are buffered and aggregated by controller serial (combining separate X/Y/Z data points)
4. A background loop pushes aggregated data to Grafana Live at 100 Hz using InfluxDB line protocol
5. Grafana Live broadcasts the data over WebSocket to connected dashboard panels

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `4318` | HTTP server port (OTLP HTTP receiver) |
| `GRAFANA_URL` | `http://grafana:3000` | Grafana base URL |
| `GRAFANA_PUSH_PATH` | `joustmania-accel` | Grafana Live stream ID |
| `GRAFANA_USER` | `admin` | Grafana basic auth username |
| `GRAFANA_PASSWORD` | `admin` | Grafana basic auth password |

| Property | Value |
|----------|-------|
| **Container port** | 4318 (internal only) |
| **Container** | `joustmania-grafana-live-publisher` |
| **Health check** | `GET /health` |
| **Memory limit** | 128 MB |

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/metrics` | OTLP HTTP metrics receiver (JSON, gzip supported) |
| `GET` | `/health` | Health check |

## Files

```
services/grafana-live-publisher/
├── Dockerfile    # Two-stage: Go build, alpine runtime
├── main.go       # OTLP parsing, aggregation, Grafana Live push
├── go.mod
└── go.sum
```

## See Also

- [OTel Collector](../otel-collector/) -- upstream metrics source
- [Grafana Dashboards](../grafana/dashboards/) -- dashboards that consume the live data
- [Observability Guide](../../docs/ARCHITECTURE.md)
