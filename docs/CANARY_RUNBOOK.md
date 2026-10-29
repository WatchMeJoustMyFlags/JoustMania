# Canary Release Runbook: Python to Rust Controller Backend Migration

Step-by-step guide for gradually migrating PS Move controllers from the Python
`PythonHidAdapter` backend to the Rust `RustServiceAdapter` gRPC backend using
OpenFeature feature flags served by flagd.

## How It Works

The `MultiplexerBackend` routes each controller through a `ControllerIOAdapter`.
On every discovery cycle (~500ms), it evaluates the `bluetooth_backend`
flag in the `controller` domain, passing the controller's serial as the
targeting key. The flag returns `"rust"` or `"python"`, and the multiplexer
opens/closes adapter handles accordingly. A change of `bluetooth_backend`
through the flag service (the canary endpoints below, or `scripts/flags.sh`)
takes effect on the next discovery cycle -- no service restart needed. The
baseline value lives in `services/flagd/controller.json`, which the flag
service loads read-only at start; nothing writes it at runtime (C-033).

### HTTP Canary Endpoints

The connect-proxy provides HTTP endpoints for controlling canary rollout.
They write `bluetooth_backend` in the
`controller` set through the flag service (a stack overlay, or a fraction
overlay for `?fraction=N`), which pushes it to flagd over the gRPC sync stream;
no service restart needed. The overlays are in memory: a flag-service restart
drops them and routing returns to the baseline.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/canary/rust` | POST | Route controllers to Rust backend |
| `/canary/python` | POST | Route controllers to Python backend (409 unless the python adapter is loaded, see below) |
| `/canary/rollback` | POST | Clear the canary overlays: every controller returns to the baseline default (`rust` since #1371) |
| `/canary/status` | GET | Show current routing flag config |

All POST endpoints support `?fraction=N` (1-100, default 100) for gradual
rollout via flagd fractional targeting (the flag service renders the
`fractional` rule). A write fails with HTTP 503 while flagd has no sync stream
to the flag service (`no_sync_stream`).

A canary that routes any controller to python (`/canary/python`, or
`/canary/rust?fraction=N` below 100, which keeps the rest on python) is refused
with HTTP 409 when the `backend` flag names no python (the default `rust`):
the controller manager has no python adapter then and would silently keep
every controller on rust (#1402, UC-052 A3). Opt in first (prerequisite 2).

**Examples:**

Envoy (host port 80) has no `/canary/` route, so a request to
`http://localhost/canary/...` reaches the dashboard instead (UC-052 A2). Call
the connect-proxy from inside the compose network, for example through
`docker compose exec` (the connect-proxy image ships `wget`; `localhost:8080`
below is the connect-proxy's own listener inside its container):

`wget -q` hides HTTP errors: a 4xx/5xx (for example the 503 `no_sync_stream`
response) prints nothing and exits 8. Check `echo $?` after each call, or
replace `-q` with `-S` to print the response headers and status.

```bash
# Route all controllers to Rust
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rust'

# Route 25% of controllers to Rust (deterministic by serial hash)
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rust?fraction=25'

# Check current routing config
docker compose exec -T connect-proxy wget -qO- http://localhost:8080/canary/status

# Immediate rollback to the baseline default (rust)
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rollback'
```

Key source files:

| File | Role |
|------|------|
| `services/controller_manager/multiplexer/multiplexer_backend.py` | `_resolve_adapter_for_serial()` routing logic |
| `services/controller_manager/multiplexer/hid_service_adapter.py` | `HidServiceAdapter`, the gRPC client both HID adapters share |
| `services/controller_manager/multiplexer/rust_adapter.py` | `RustServiceAdapter` (gRPC client to rust-hid) |
| `services/controller_manager/multiplexer/python_hid_adapter.py` | `PythonHidAdapter` (gRPC client to python-hid) |
| `services/controller_manager/backend_factory.py` | Adapter instantiation, `backend` flag |
| `services/flagd/controller.json` | Baseline flag configuration (read-only; loaded by the flag service at start) |
| `services/connect-proxy/canary.go`, `flagservice.go` | Canary endpoints and their flag-service client |
| `services/controller_manager/metrics.py` | All controller metrics definitions |

---

## Prerequisites

1. **Docker Compose stack running** -- all services healthy:
   ```bash
   docker compose ps
   ```

2. **rust-hid and python-hid services deployed and healthy** -- since #1371
   the default `backend` flag is `"rust"` and python-hid is opt-in. A canary
   between the two backends needs both adapters inside `MultiplexerBackend`:
   start python-hid with its compose profile, set the `backend` flag to
   `python_rust`, and restart controller-manager (the flag is read once at
   startup). `backend` is not in the flag service's mutable registry, so this
   is a baseline edit followed by a planned restart of the flag service and
   flagd. The `bluetooth_backend` flag then controls which adapter handles
   each serial (default `rust`):
   ```bash
   COMPOSE_PROFILES=python-hid docker compose up -d python-hid
   # services/flagd/controller.json: "backend" defaultVariant -> "python_rust"
   scripts/flags.sh restart              # flag-service, then flagd
   docker compose restart controller-manager
   ```

3. **Flag service and flagd running**, flagd connected to the flag service's
   sync stream:
   ```bash
   scripts/flags.sh state                # sync stream count must be 1
   scripts/flags.sh get controller bluetooth_backend
   ```

4. **At least one PS Move controller connected** and showing up in the
   Controllers & Bluetooth dashboard (`/grafana/d/controller-overview`).

---

## Step 1: Verify Baseline

Before routing any controller to the Rust backend, record baseline metrics
while all controllers are on `python`.

### Check Grafana dashboards

Open the observability stack at `http://localhost/`:

- **Controllers & Bluetooth** (`/grafana/d/controller-overview`) -- verify all
  controllers connected, LEDs responsive, and every controller on `python` in
  the "Controllers per Backend" panel
- **Infrastructure & Services** (`/grafana/d/joustmania-service-health`) -- verify controller-manager CPU/memory normal

### Confirm all controllers on python

Query VictoriaMetrics (Grafana Explore):

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

### Confirm routing flag is defaulting to the stable backend

```promql
# All routing decisions should show the bluetooth_backend default
# (adapter="rust" since #1371; adapter="python" if you set the default to python for this canary)
controller_routing_decisions_total
```

---

## Step 2: Route Controllers to Rust

### Using the canary endpoint (recommended)

```bash
# Route a small fraction (e.g., 25%) of controllers to Rust
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rust?fraction=25'
```

This uses flagd's fractional targeting to deterministically route ~25% of
controllers (by serial hash) to the Rust backend.

### Manual single-controller targeting (alternative)

The flag service accepts only stack and fraction scopes for
`bluetooth_backend` (flags.md §13), so routing one specific serial (e.g.,
`aa:bb:cc:dd:ee:ff`) is a reviewed edit of the baseline
`services/flagd/controller.json`, followed by `scripts/flags.sh restart`:

```json
"bluetooth_backend": {
  "state": "ENABLED",
  "variants": {
    "python": "python",
    "rust": "rust"
  },
  "defaultVariant": "python",
  "targeting": {
    "if": [
      { "==": [{ "var": "$flagd.flagKey" }, "bluetooth_backend"] },
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

`scripts/flags.sh restart` restarts the flag service and then flagd, which
serves the new baseline within 5 s. Any canary overlay set before the restart
is gone.

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

Run these queries in Grafana Explore or the VictoriaMetrics UI at
`http://localhost/victoria-metrics/vmui/`:

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

**Discovery passes** (every pass is a full enumeration, #2352):

```promql
# Discovery passes per second (about 1 at the 1 s interval)
rate(controller_discovery_full_enumerate_total[5m])
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

After the initial canary is stable, expand using the canary endpoint:

```bash
# 25% rollout
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rust?fraction=25'

# 50% rollout
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rust?fraction=50'

# 100% rollout
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rust'
```

### Between each stage

- Check status: `docker compose exec -T connect-proxy wget -qO- http://localhost:8080/canary/status`
- Watch the routing decisions metric update:
  ```promql
  sum by (adapter) (rate(controller_routing_decisions_total[2m]))
  ```
- Monitor for 10-15 minutes at each stage before proceeding
- Check that all controllers maintain stable `controller_state_update_hz`

---

## Step 5: Rollback

If issues arise at any stage, roll back through the canary endpoint.

### Immediate rollback

```bash
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rollback'
```

The rollback clears the stack and fraction overlays, so all controllers switch
back to the baseline default of `bluetooth_backend` (`rust` since #1371) within
one discovery cycle (~500ms). To return everyone to python instead, call
`/canary/python` (needs the python adapter loaded). If the python baseline came from a
`/canary/python` stack overlay, rollback goes to rust, not python.
**No service restart needed.**

### Verify rollback

```bash
docker compose logs controller-manager --tail=20 | grep "Switched"
```

Controllers that were on the canary backend switch back to the baseline
default (`rust` since #1371), e.g. after a python canary:
```
Switched aa:bb:cc:dd:ee:ff: python -> rust
```

Confirm with metrics:

```promql
# No controller left on the canary backend (here: python)
controller_backend_info{backend="python"} == 1
# Should return no results
```

```promql
# All routing decisions back on the baseline default
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

```bash
docker compose exec -T connect-proxy wget -qO- --post-data= 'http://localhost:8080/canary/rust'
```

This sets an in-memory stack overlay on `"rust"` and clears any fraction
overlay. The overlay lasts until the flag service restarts; to make `rust`
the permanent default, change `bluetooth_backend`'s `defaultVariant` in the
baseline `services/flagd/controller.json` in a reviewed PR and run
`scripts/flags.sh restart`.

### Update backend flag (optional)

If you no longer need the python adapter instantiated, change the
`backend` flag in the baseline `services/flagd/controller.json` to `"rust"`
only (reviewed edit, then `scripts/flags.sh restart`):

```json
"backend": {
  "state": "ENABLED",
  "variants": {
    "rust": "rust",
    ...
  },
  "defaultVariant": "rust"
}
```

This requires a controller-manager restart (`docker compose restart
controller-manager`) since `backend` is read once at startup.

### Make rust-hid a required dependency (optional)

Add a health check dependency in `docker-compose.yml` so controller-manager
waits for rust-hid:

```yaml
controller-manager:
  depends_on:
    rust-hid:
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

# Restart controller-manager (needed after backend flag change)
docker compose restart controller-manager

# Flag service view: sync streams, current bluetooth_backend overlays
scripts/flags.sh state
scripts/flags.sh get controller bluetooth_backend

# flagd sync events
docker compose logs flagd --tail=20
```

### PromQL queries for debugging

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

# Discovery passes
rate(controller_discovery_full_enumerate_total[5m])

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
| All controllers stuck on python despite targeting | `backend` flag doesn't include `rust` | Verify flag value includes `rust` (e.g., `"python,rust"`); restart controller-manager |
| Canary endpoint returns 503 | flagd has no sync stream to the flag service (`no_sync_stream`) | `scripts/flags.sh state`; check `docker compose logs flagd flag-service`; `scripts/flags.sh restart` |
| Canary routing gone after a restart | Canary overlays are in memory; a flag-service restart drops them | Re-run the canary endpoint, or make the value the baseline (Step 6) |

### Grafana dashboards

| Dashboard | What to check |
|-----------|---------------|
| `controller-overview` (Controllers & Bluetooth) | Connected controllers, controllers per backend (`controller_backend_info == 1`), health table (backend, adapter, battery, RSSI, poll rate, connection quality), input lag p95, poll drops and errors, LED colors; the collapsed "Bluetooth and Adapters (Pi only)" row has the per-adapter load, signal and poll rate |
| `joustmania-service-health` (Infrastructure & Services) | CPU, memory, gRPC latency for controller-manager |

### Alerting

No Prometheus alert rules ship (FR-083 Rejected, #1425): watch the dashboards
above during migration, in particular `controller_connected`, the input lag
panels (`controller_input_lag_seconds`) on Controllers & Bluetooth, CPU, and
`rate(controller_poll_errors_total[2m])` per backend.
