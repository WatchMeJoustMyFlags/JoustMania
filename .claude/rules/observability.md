# Observability Guide

## Dashboards

Located in `services/grafana/dashboards/`:

| Dashboard | Purpose |
|-----------|---------|
| `service-health-overview.json` | All services up/down, CPU, memory |
| `game-quality.json` | Frame timing, jitter, dropped frames |
| `game-analytics.json` | Game duration, deaths, win rates |
| `player-insights.json` | Per-player movement, deaths, playstyle |
| `controller-overview.json` | Battery levels, connection status |
| `controller-maintenance.json` | Hardware health, signal strength |
| `host-metrics.json` | Raspberry Pi CPU, memory, temperature |
| `system-overview.json` | High-level system status |
| `bluetooth-adapter.json` | Bluetooth adapter metrics |
| `cache-performance.json` | State cache hit rates |

## Adding Metrics

### Define in `metrics.py`

```python
from lib.otel_metrics import Counter, Gauge, Histogram

# Counter - cumulative, only increases
my_events_total = Counter(
    "my_events_total",
    "Total events processed",
    ["event_type"]  # Labels
)

# Gauge - current value, can go up/down
my_active_connections = Gauge(
    "my_active_connections",
    "Currently active connections"
)

# Histogram - distribution of values
my_latency_seconds = Histogram(
    "my_latency_seconds",
    "Request latency",
    buckets=[0.01, 0.05, 0.1, 0.5, 1.0]
)
```

### Use in Code

```python
# Counter
metrics.my_events_total.labels(event_type="click").inc()

# Gauge
metrics.my_active_connections.set(len(connections))
metrics.my_active_connections.inc()  # +1
metrics.my_active_connections.dec()  # -1

# Histogram
metrics.my_latency_seconds.observe(0.042)
with metrics.my_latency_seconds.time():
    do_work()  # Auto-records duration
```

## Metric Naming Conventions

- Prefix with service: `game_`, `controller_`, `menu_`
- Use snake_case
- End counters with `_total`
- End histograms with `_seconds`, `_bytes`, etc.
- Include units in name

### Standard Process Metrics

All services (Python and infra) use the Prometheus/Go naming convention:

```python
# In each service's metrics.py
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")
process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")
process_threads = Gauge("process_threads", "Number of active threads")
```

Dashboard PromQL patterns:
- CPU %: `rate(process_cpu_seconds_total[5m]) * 100`
- Memory: `process_resident_memory_bytes` (Grafana auto-formats bytes)

Infra services (OTel Collector, VictoriaMetrics, Prometheus, flagd) expose these natively.
The OTel Collector's own metrics (`otelcol_process_*`) are relabeled to standard names
in the `prometheus/infra` scrape config.

## Dashboard Updates

After adding metrics:
1. Metrics auto-export via OTEL (no config needed)
2. Edit dashboard JSON or use Grafana UI
3. Export and save to `services/grafana/dashboards/`

## Tracing

### Add Spans

```python
from lib.otel_tracing import tracer

with tracer.start_as_current_span("my_operation") as span:
    span.set_attribute("player.serial", serial)
    span.set_attribute("game.mode", mode)
    # ... do work
```

### Span Naming

- Use dot notation: `game.process_death`
- Include service context: `menu.handle_button`
- Be specific: `controller.poll_batch` not just `poll`

## Viewing Traces

1. Open Jaeger: http://localhost:16686
2. Select service from dropdown
3. Search by:
   - Service name
   - Operation name
   - Tags (e.g., `player.serial=XX:XX`)
   - Duration (find slow operations)

## Alerts

Defined in `services/prometheus/alerts.yml`:

```yaml
- alert: ServiceDown
  expr: up{job="menu"} == 0
  for: 30s
  labels:
    severity: critical
  annotations:
    summary: "Menu service is down"
```

## Log Aggregation

Logs go to Loki, viewable in Grafana:
1. Go to Explore
2. Select Loki datasource
3. Query: `{service="menu"} |= "error"`
