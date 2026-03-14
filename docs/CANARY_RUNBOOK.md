# Canary Release Runbook: Python to Rust Controller Backend Migration

Step-by-step guide for gradually migrating PS Move controllers from the Python
`PythonHidAdapter` backend to the Rust `RustServiceAdapter` gRPC backend using
OpenFeature feature flags served by flagd.

## How It Works

The `MultiplexerBackend` routes each controller through a `ControllerIOAdapter`.
On every discovery cycle (~500ms), it evaluates the `controller_adapter_routing`
flag in the `performance` domain, passing the controller's serial as the
targeting key. The flag returns `"rust"` or `"python"`, and the multiplexer
opens/closes adapter handles accordingly. Changing the flag config in
`services/flagd/performance.json` takes effect on the next discovery cycle --
no service restart needed.

Key source files:

| File | Role |
|------|------|
| `services/controller_manager/multiplexer/multiplexer_backend.py` | `_resolve_adapter_for_serial()` routing logic |
| `services/controller_manager/multiplexer/rust_adapter.py` | `RustServiceAdapter` (gRPC client to rust-hid) |
| `services/controller_manager/multiplexer/python_hid_adapter.py` | `PythonHidAdapter` (gRPC client to python-hid) |
| `services/controller_manager/backend_factory.py` | Adapter instantiation, `controller_backend` flag |
| `services/flagd/performance.json` | Feature flag configuration (file-watched by flagd) |
| `services/controller_manager/metrics.py` | All controller metrics definitions |

---

## Prerequisites

1. **Docker Compose stack running** -- all services healthy:
   ```bash
   docker compose ps
   ```

2. **rust-hid and python-hid services deployed and healthy** -- the default
   `controller_backend` flag is `"python,rust"`, which creates both adapters
   inside `MultiplexerBackend`. The `controller_adapter_routing` flag then
   controls which adapter handles each serial. If you changed
   `controller_backend` from the default, ensure it includes both `python` and
   `rust`, then restart controller-manager (it is read once at startup):
   ```bash
   docker compose restart controller-manager
   ```

3. **flagd running** and watching `services/flagd/performance.json`:
   ```bash
   docker compose logs flagd --tail=5
   # Should show "watching file" or "flag configuration changed"
   ```

4. **At least one PS Move controller connected** and showing up in the
   controller-overview dashboard.

---

## Step 1: Verify Baseline

Before routing any controller to the Rust backend, record baseline metrics
while all controllers are on `python`.

### Check Grafana dashboards

Open the observability stack at `http://localhost:8080/`:

- **Controller Overview** (`/grafana/`) -- verify all controllers connected, LEDs
  responsive
- **Service Health Overview** -- verify controller-manager CPU/memory normal

### Confirm all controllers on python

Query Prometheus / VictoriaMetrics:

```promql
controller_backend_info == 1
```

All results should have `backend="python"`. No results should show
`backend="rust"`.

### Record baseline numbers

```promql
# Poll batch duration (p95)
histogram_quantile(0.95, rate(controller_poll_batch_duration_seconds_bucket[5m]))

# Poll drops per controller
rate(controller_poll_drops_total[5m])

# Poll errors per controller
rate(controller_poll_errors_total[5m])

# Input latency (p95)
histogram_quantile(0.95, rate(controller_input_lag_seconds_bucket[5m]))

# Discovery cycle rate
rate(controller_discovery_full_enumerate_total[5m])
```

Write down these values. They are your comparison baseline.

### Confirm routing flag is defaulting to python

```promql
# All routing decisions should show adapter="python"
controller_routing_decisions_total
```

---

## Step 2: Route a Single Controller to Rust

Pick one controller serial (e.g., `aa:bb:cc:dd:ee:ff`) and route it to the
Rust backend.

### Edit the flag config

Edit `services/flagd/performance.json`. Update the `controller_adapter_routing`
flag's `targeting` block:

```json
"controller_adapter_routing": {
  "state": "ENABLED",
  "variants": {
    "python": "python",
    "rust": "rust"
  },
  "defaultVariant": "python",
  "targeting": {
    "if": [
      { "==": [{ "var": "$flagd.flagKey" }, "controller_adapter_routing"] },
      {
        "if": [
          { "in": [{ "var": "targetingKey" }, ["aa:bb:cc:dd:ee:ff"]] },
          "rust",
          null
        ]
      },
      null
    ]
  }
}
```

Save the file. flagd watches the directory via inotify and reloads
automatically -- no restart needed.

### Verify the switch

Within ~500ms (one discovery cycle), the controller should switch adapters.
Check the controller-manager logs:

```bash
docker compose logs controller-manager --tail=20 | grep "Switched\|preferred adapter"
```

You should see a log line like:
```
Switched aa:bb:cc:dd:ee:ff: python -> rust
```

Confirm with metrics:

```promql
# Should now show backend="rust" for your test serial
controller_backend_info{serial="aa:bb:cc:dd:ee:ff"}
```

```promql
# Routing decision should show adapter="rust", method="targeted"
controller_routing_decisions_total{serial="aa:bb:cc:dd:ee:ff", adapter="rust", method="targeted"}
```

---

## Step 3: Monitor

### Key metrics to watch

Run these queries in Grafana or the Prometheus UI at
`http://localhost:8080/prometheus/`:

**Poll batch duration by backend** (join with backend info):

```promql
# Overall poll batch duration (p95) -- covers all controllers in a batch
histogram_quantile(0.95, rate(controller_poll_batch_duration_seconds_bucket[5m]))
```

**Per-controller poll health** (compare your canary serial to others):

```promql
# Poll drops -- Rust controller vs python controllers
rate(controller_poll_drops_total{serial="aa:bb:cc:dd:ee:ff"}[5m])
rate(controller_poll_drops_total{serial!="aa:bb:cc:dd:ee:ff"}[5m])
```

```promql
# Poll errors
rate(controller_poll_errors_total{serial="aa:bb:cc:dd:ee:ff"}[5m])
rate(controller_poll_errors_total{serial!="aa:bb:cc:dd:ee:ff"}[5m])
```

**Input latency comparison**:

```promql
# P95 input latency for the Rust-routed controller
histogram_quantile(0.95, rate(controller_input_lag_seconds_bucket{serial="aa:bb:cc:dd:ee:ff"}[5m]))

# P95 input latency for python controllers
histogram_quantile(0.95, rate(controller_input_lag_seconds_bucket{serial!="aa:bb:cc:dd:ee:ff"}[5m]))
```

**Routing decision breakdown**:

```promql
# See all routing decisions by adapter and method
sum by (adapter, method) (rate(controller_routing_decisions_total[5m]))
```

**Discovery cycle split**:

```promql
# Full enumerations vs verify-only cycles
rate(controller_discovery_full_enumerate_total[5m])
rate(controller_discovery_verify_only_total[5m])
```

**Controller state update frequency**:

```promql
# Compare update Hz across controllers
controller_state_update_hz
```

### What to look for

| Metric | Healthy | Investigate |
|--------|---------|-------------|
| `controller_poll_drops_total` rate | < 0.1/s | > 1/s sustained |
| `controller_poll_errors_total` rate | 0 | Any non-zero |
| Input latency p95 | < 20ms | > 50ms |
| `controller_state_update_hz` | Near target (60 or 100) | Drops below 50 |
| LED responsiveness | Visually immediate | Visible delay or flicker |

### Soak time

Run the canary for **15-30 minutes** with active controller use (button
presses, motion). If running a game session, complete at least one full game
cycle.

---

## Step 4: Gradual Rollout

After the single-controller canary is stable, expand to more controllers using
flagd's `fractionalEvaluation` operator.

### 25% rollout

```json
"controller_adapter_routing": {
  "state": "ENABLED",
  "variants": {
    "python": "python",
    "rust": "rust"
  },
  "defaultVariant": "python",
  "targeting": {
    "fractionalEvaluation": [
      { "var": "targetingKey" },
      ["rust", 25],
      ["python",75]
    ]
  }
}
```

### 50% rollout

```json
"controller_adapter_routing": {
  "state": "ENABLED",
  "variants": {
    "python": "python",
    "rust": "rust"
  },
  "defaultVariant": "python",
  "targeting": {
    "fractionalEvaluation": [
      { "var": "targetingKey" },
      ["rust", 50],
      ["python",50]
    ]
  }
}
```

### 100% rollout (via targeting)

```json
"controller_adapter_routing": {
  "state": "ENABLED",
  "variants": {
    "python": "python",
    "rust": "rust"
  },
  "defaultVariant": "python",
  "targeting": {
    "fractionalEvaluation": [
      { "var": "targetingKey" },
      ["rust", 100],
      ["python",0]
    ]
  }
}
```

### Between each stage

- Wait for flagd to reload (check logs: `docker compose logs flagd --tail=5`)
- Watch the routing decisions metric update:
  ```promql
  sum by (adapter) (rate(controller_routing_decisions_total[2m]))
  ```
- Monitor for 10-15 minutes at each stage before proceeding
- Check that all controllers maintain stable `controller_state_update_hz`

---

## Step 5: Rollback

If issues arise at any stage, roll back by reverting the flag config.

### Immediate rollback

Set `defaultVariant` back to `"python"` and clear targeting:

```json
"controller_adapter_routing": {
  "state": "ENABLED",
  "variants": {
    "python": "python",
    "rust": "rust"
  },
  "defaultVariant": "python",
  "targeting": {}
}
```

Save the file. All controllers switch back within one discovery cycle (~500ms).
**No service restart needed.**

### Verify rollback

```bash
docker compose logs controller-manager --tail=20 | grep "Switched"
```

You should see lines like:
```
Switched aa:bb:cc:dd:ee:ff: rust -> python
```

Confirm with metrics:

```promql
# All controllers back on python
controller_backend_info{backend="rust"} == 1
# Should return no results
```

```promql
# All routing decisions back to python
sum by (adapter) (rate(controller_routing_decisions_total[1m]))
```

### Fallback behavior

If `RustServiceAdapter.open()` fails for a serial, `MultiplexerBackend`
automatically falls back to the discovery adapter (python). The routing
decision is recorded with `method="fallback"`:

```promql
controller_routing_decisions_total{method="fallback"}
```

A non-zero fallback rate indicates the Rust service is having issues opening
controller handles.

---

## Step 6: Full Rollout

Once 100% of controllers have been stable on the Rust backend for a full
session (or longer), make it the permanent default.

### Set default to rust

```json
"controller_adapter_routing": {
  "state": "ENABLED",
  "variants": {
    "python": "python",
    "rust": "rust"
  },
  "defaultVariant": "rust",
  "targeting": {}
}
```

### Update controller_backend flag (optional)

If you no longer need the python adapter instantiated, change the
`controller_backend` flag to `"rust"` only:

```json
"controller_backend": {
  "state": "ENABLED",
  "variants": {
    "rust": "rust",
    ...
  },
  "defaultVariant": "rust"
}
```

This requires a controller-manager restart (`docker compose restart
controller-manager`) since `controller_backend` is read once at startup.

### Make rust-hid a required dependency (optional)

Add a health check dependency in `docker-compose.yml` so controller-manager
waits for rust-hid:

```yaml
controller-manager:
  depends_on:
    rust-hid:
      condition: service_healthy
    redis:
      condition: service_healthy
    flagd:
      condition: service_started
```

---

## Appendix

### Useful Docker commands

```bash
# View rust-hid logs
docker compose logs rust-hid --tail=50 -f

# View controller-manager logs (filtered for routing)
docker compose logs controller-manager --tail=100 | grep -E "Switched|adapter|routing"

# Check service health
docker compose ps

# Restart controller-manager (needed after controller_backend flag change)
docker compose restart controller-manager

# View flagd flag reload events
docker compose logs flagd --tail=20
```

### Prometheus queries for debugging

```promql
# Which backend owns each controller right now?
controller_backend_info == 1

# Routing decision rate by adapter and method
sum by (adapter, method) (rate(controller_routing_decisions_total[5m]))

# Fallback rate (Rust adapter open() failures)
rate(controller_routing_decisions_total{method="fallback"}[5m])

# Poll health by serial
rate(controller_poll_drops_total[5m])
rate(controller_poll_errors_total[5m])

# Discovery throttle: full vs verify-only cycles
rate(controller_discovery_full_enumerate_total[5m])
rate(controller_discovery_verify_only_total[5m])

# Active controller count
active_controllers_total

# Controller-manager CPU and memory
rate(process_cpu_seconds_total{job=~".*controller-manager.*"}[5m]) * 100
process_resident_memory_bytes{job=~".*controller-manager.*"}
```

### Common failure modes

| Symptom | Likely Cause | Resolution |
|---------|-------------|------------|
| `method="fallback"` routing decisions increasing | rust-hid service unhealthy or not running | Check `docker compose ps rust-hid`, check logs |
| Controller LEDs stop updating after switch | `set_output()` failing on Rust adapter | Check `_led_failures` via health counters in stream; rollback |
| `controller_state_update_hz` drops to 0 for a serial | `poll()` returning None consistently | Check `controller_poll_drops_total`; may indicate gRPC timeout to rust-hid |
| All controllers stuck on python despite targeting | `controller_backend` flag doesn't include `rust` | Verify flag value includes `rust` (e.g., `"python,rust"`); restart controller-manager |
| flagd not picking up config changes | File saved with atomic rename across filesystems | Verify `services/flagd/` directory is mounted, check flagd logs |
| `NotImplementedError` in controller-manager logs | `RustServiceAdapter` stub not yet replaced (#612) | The Rust adapter is still a stub; wait for #612 to be merged |

### Grafana dashboards

| Dashboard | What to check |
|-----------|---------------|
| `controller-overview` | Connected controllers, battery, LED colors |
| `controller-maintenance` | Poll drops, poll errors, backend info |
| `service-health-overview` | CPU, memory, gRPC latency for controller-manager |
| `performance-experiment` | Backend comparison metrics |
| `bluetooth-adapter` | BT adapter affinity per controller |

### Alerting

Existing alerts in `services/prometheus/alerts.yml` that are relevant during
migration:

- **ControllerDisconnected** -- fires if `controller_connected == 0` for 30s
- **HighInputLatency** -- fires if p95 input latency exceeds 50ms for 2m
- **HighCPUUsage** -- fires if CPU usage exceeds 80% for 2m
- **ServiceDown** -- fires if Prometheus cannot scrape a service for 1m

Consider adding a canary-specific alert during migration:

```yaml
- alert: RustBackendHighPollErrors
  expr: rate(controller_poll_errors_total[2m]) > 0.5
    and on(serial) controller_backend_info{backend="rust"} == 1
  for: 1m
  labels:
    severity: warning
  annotations:
    summary: "Rust backend poll errors on {{$labels.serial}}"
    description: "Controller on Rust backend has poll error rate {{$value}}/s."
```
