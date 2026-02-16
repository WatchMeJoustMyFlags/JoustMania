# Feature Flags with Event-Driven Updates

## Overview

JoustMania uses [OpenFeature](https://openfeature.dev/) with [flagd](https://flagd.dev/) for runtime configuration management. Feature flags allow changing game behavior without redeploying code.

**Key Features:**
- **Event-driven updates** - Changes propagate instantly via gRPC streams (no polling)
- **Type-safe evaluation** - Flags are strongly typed (boolean, string, integer)
- **Observable** - Metrics and logging for flag evaluations and changes
- **Domain-scoped** - Three flag files with `flagSetId` metadata for isolation
- **Developer-friendly** - Edit flag files in `services/flagd/` and see changes in <100ms

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────┐
│  Flag Files (services/flagd/)                           │
│  - performance.json    (flagSetId: "performance")       │
│  - game_settings.json  (flagSetId: "game_settings")     │
│  - user_preferences.json (flagSetId: "user_preferences")│
│  - Watched by flagd via inotify (Linux) or polling      │
└──────────────────┬──────────────────────────────────────┘
                   │
                   │ File change detected
                   ▼
┌─────────────────────────────────────────────────────────┐
│  flagd service (port 8015)                              │
│  - gRPC server implementing OpenFeature Flagd Protocol  │
│  - Loads all 3 flag files via --uri arguments            │
│  - Uses flagSetId metadata for domain scoping           │
│  - Pushes flag updates when any file changes            │
└──────────────────┬──────────────────────────────────────┘
                   │
                   │ gRPC bidirectional stream
                   │ SyncFlags() RPC
                   ▼
┌─────────────────────────────────────────────────────────┐
│  OpenFeature Domain-Scoped Providers                    │
│  - "performance" domain (game-coordinator)              │
│  - "game_settings" domain (menu)                        │
│  - "user_preferences" domain (menu, audio)              │
│  - Each provider uses selector="flagSetId=<id>"         │
│  - Connects to flagd via IN_PROCESS resolver            │
│  - Emits PROVIDER_CONFIGURATION_CHANGED event           │
└──────────────────┬──────────────────────────────────────┘
                   │
                   │ Event callback / Direct reads
                   ▼
┌─────────────────────────────────────────────────────────┐
│  Service Consumers                                      │
│  - RuntimeConfigManager: event-driven cache (perf)      │
│  - Menu: reads game_settings + user_preferences         │
│  - Audio: reads user_preferences (voice, audio on/off)  │
│  - FlagConfigWriter: atomic writes to flag files        │
└─────────────────────────────────────────────────────────┘
```

**Latency:** Flag file change → Config update = **<100ms**

### How Event-Driven Updates Work

1. **Startup:** RuntimeConfigManager registers a callback for `PROVIDER_CONFIGURATION_CHANGED` events
2. **Normal Operation:** Config is cached and served from memory (zero overhead)
3. **Flag Change:** When any flag file is edited:
   - flagd detects the file change
   - flagd pushes update to all connected clients via gRPC stream
   - OpenFeature provider emits `PROVIDER_CONFIGURATION_CHANGED` event
   - RuntimeConfigManager callback fires, re-evaluates flags, updates cache
   - Logs show `🚩 Feature flags changed: ['update_frequency_hz']`
   - Metrics increment `game_flag_configuration_changes_total`

**Why event-driven?**
- **No polling overhead** - Config is read from cache, not from flagd on every game loop iteration
- **Instant updates** - Changes propagate in <100ms instead of waiting for next poll interval
- **Lower CPU usage** - No periodic flag evaluations
- **No rate limiting** - Avoids "too_many_pings" errors from excessive gRPC keepalives

### Thread Safety

Flag change events fire in a background thread. RuntimeConfigManager uses `threading.RLock()` to protect the config cache during reads/writes.

## How to Test

### 1. Start the System

```bash
# On Raspberry Pi
cd ~/JoustMania
docker compose up -d

# Verify flagd is running
docker compose ps flagd
```

### 2. Watch for Flag Events

Open a terminal and follow logs:

```bash
docker compose logs -f game-coordinator | grep -E "🎯|🚩|Flag"
```

You should see on startup:
```
INFO - Feature flag client initialized
INFO - Registered PROVIDER_CONFIGURATION_CHANGED event handler
```

### 3. Edit Flag Values

Open the flags file:

```bash
# On Raspberry Pi
nano services/flagd/performance.json
```

Change a flag value (e.g., `update_frequency_hz` from `low` to `high`):

```json
{
  "flags": {
    "update_frequency_hz": {
      "state": "ENABLED",
      "variants": {
        "low": 15,
        "medium": 30,
        "high": 60
      },
      "defaultVariant": "high"  // ← Change this
    }
  }
}
```

Save the file (Ctrl+O, Enter, Ctrl+X).

### 4. Verify Event Detection

Within **1 second**, you should see in the logs:

```
🚩 Feature flags changed: ['update_frequency_hz', 'streaming_mode', 'sensitivity_mode', 'enable_adaptive_rewards']
🎯 Config updated: update_frequency_hz 15 → 60 Hz
```

**Note:** All flags are listed in the change event, but only modified flags show before/after values.

### 5. Test During Gameplay

Start a game and edit flags while playing:

1. Start a game with 2+ controllers
2. In another terminal, change `update_frequency_hz` to `low` (15 Hz)
3. Observe gameplay becomes less responsive
4. Change back to `high` (60 Hz)
5. Observe gameplay becomes more responsive

The game will adapt in real-time without restarting.

### 6. Check Metrics

View metrics in Prometheus at `http://localhost:9090`:

**Flag evaluations by key:**
```promql
rate(game_flag_evaluations_total[1m])
```

**Configuration change events:**
```promql
game_flag_configuration_changes_total
```

**Current update frequency:**
```promql
game_current_update_frequency_hz
```

Or view in Grafana's **Feature Flags** dashboard at `http://localhost:3000`.

## Available Flags

### Performance Flags (`services/flagd/performance.json`)

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `update_frequency_hz` | integer | 15, 30, 60 | Game loop update frequency |
| `streaming_mode` | string | standard, filtered, aggressive | Controller data streaming mode |
| `sensitivity_mode` | string | low, medium, high | Death detection sensitivity |
| `enable_adaptive_rewards` | boolean | true, false | Enable dynamic reward scaling |
| `chaos_fault_type` | string | none, poll_drop, accel_spike, led_failure, disconnect | Chaos fault injection for controllers (see [ChaosAdapter](architecture/controller-backends.md#chaosadapter-fault-injection)) |

### Game Settings Flags (`services/flagd/game_settings.json`)

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `sensitivity` | integer | 0-4 | Death detection sensitivity (ultra_slow to ultra_fast) |
| `num_teams` | integer | 2-6 | Number of teams for team modes |
| `random_assignment` | boolean | true, false | Random team assignment |
| `nonstop_time_limit` | integer | 0, 60-300 | Nonstop Joust time limit in seconds |
| `invincibility` | float | 2.0-8.0 | Invincibility duration for tournament modes |
| `fight_club_min_rounds` | integer | 5-20 | Minimum rounds for Fight Club |
| `werewolf_reveal_time` | float | 20.0-60.0 | Werewolf reveal time in seconds |
| `force_all_start` | boolean | true, false | Force start with all connected controllers |

### User Preferences Flags (`services/flagd/user_preferences.json`)

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `menu_voice` | string | aaron, ivy | Voice actor for announcements |
| `play_audio` | boolean | true, false | Enable/disable audio output |
| `current_game` | string | game mode names | Currently selected game mode |
| `game_instructions` | boolean | true, false | Show game instructions |

### Adding New Flags

1. Choose the appropriate flag file based on domain:
   - `performance.json` - Runtime performance tuning
   - `game_settings.json` - Game configuration (admin-adjustable)
   - `user_preferences.json` - User preferences (persist across sessions)

2. Add the flag definition with `"state": "ENABLED"` and variants.

3. In your service code, initialize the domain and get a client:
   ```python
   from lib.feature_flags import init_flag_domain, get_flag_client

   init_flag_domain("game_settings")
   client = get_flag_client("game_settings")
   value = client.get_integer_value("my_new_flag", default_value)
   ```

4. No restart required - flagd will detect the file change automatically.

## Metrics

The following metrics are exported to Prometheus:

### `game_flag_evaluations_total{flag_key}`

**Type:** Counter
**Labels:** `flag_key` (e.g., "update_frequency_hz")
**Description:** Total number of times each flag has been evaluated

**Usage:**
```promql
# Evaluation rate per flag
rate(game_flag_evaluations_total[1m])

# Most frequently evaluated flags
topk(5, sum by (flag_key) (game_flag_evaluations_total))
```

### `game_flag_configuration_changes_total`

**Type:** Counter
**Description:** Total number of PROVIDER_CONFIGURATION_CHANGED events received

**Usage:**
```promql
# Configuration changes over time
increase(game_flag_configuration_changes_total[1h])

# Alert on unexpected changes
rate(game_flag_configuration_changes_total[5m]) > 2
```

### `game_current_update_frequency_hz`

**Type:** Gauge
**Description:** Current configured update frequency in Hz (15, 30, or 60)

**Usage:**
```promql
# Current value
game_current_update_frequency_hz

# Alert if too low
game_current_update_frequency_hz < 30
```

## Troubleshooting

### No flag change events detected

**Symptoms:** Edit a flag file but no `🚩 Feature flags changed` log appears

**Diagnosis:**
```bash
# Check flagd is running
docker compose ps flagd

# Check flagd logs
docker compose logs flagd --tail=50

# Verify file is being watched
docker compose exec flagd ls -l /etc/flagd/
```

**Solutions:**
- Ensure flagd container is healthy
- Verify flag files are mounted correctly in docker-compose.yml
- Check file permissions (must be readable by flagd)

### "Could not initialize feature flags" error

**Symptoms:** Service starts but shows import error

**Diagnosis:**
```bash
# Check if openfeature packages are installed
docker compose exec game-coordinator pip list | grep openfeature
```

**Solutions:**
- Rebuild image: `docker compose up -d --build game-coordinator`
- Verify `pyproject.toml` includes `openfeature-sdk` and `openfeature-provider-flagd`

### "too_many_pings" errors (HTTP 429)

**Symptoms:** Logs show rate limiting errors from flagd

**Root Cause:** This should NOT happen with event-driven updates. If you see this:

**Diagnosis:**
```bash
# Check if event handler is registered
docker compose logs game-coordinator | grep "Registered PROVIDER_CONFIGURATION_CHANGED"
```

**Solutions:**
- Verify RuntimeConfigManager registers the event handler on startup
- Check that `get_config()` returns cached values, not re-evaluating flags

### Metrics not showing up in Prometheus

**Symptoms:** Queries return no data

**Diagnosis:**
```bash
# Check metrics endpoint
curl -s localhost:9090/api/v1/label/__name__/values | jq -r '.data[]' | grep game_flag

# Check OTEL collector is receiving metrics
docker compose logs otel-collector | grep game-coordinator
```

**Solutions:**
- Wait 10 seconds (metrics export interval)
- Verify Prometheus is scraping the service (check Targets page)
- Check OTEL_EXPORTER_OTLP_ENDPOINT is set correctly

### Flag changes don't affect gameplay

**Symptoms:** Edit flags but game behavior doesn't change

**Diagnosis:**
```bash
# Verify event is detected
docker compose logs game-coordinator | tail -20

# Check if config is being applied
# Should see "🎯 Config updated" logs
```

**Solutions:**
- Ensure flag key matches exactly in code and JSON
- Verify the config value is actually being used in game logic
- Check if game needs restart to pick up initial config

## Development

### Running Tests

```bash
# Unit tests
cd services/game_coordinator
uv run pytest tests/test_runtime_config.py -v

# Integration tests with flagd
docker compose -f docker-compose.test.yml up --abort-on-container-exit
```

### Local Development Without flagd

If flagd is unavailable, the system gracefully degrades:

```python
try:
    from lib.feature_flags import init_flag_domain, get_flag_client
    init_flag_domain("performance")
    self.flags = get_flag_client("performance")
except ImportError:
    logger.warning("Feature flags disabled - using defaults")
    self.flags = None
```

Default values are used when flags can't be evaluated.

### Debugging Flag Evaluation

Enable DEBUG logging to see every flag evaluation:

```bash
# In docker-compose.yml
environment:
  - LOG_LEVEL=DEBUG
```

```
DEBUG - Evaluating flag: update_frequency_hz
DEBUG - Flag value: 60
```

## References

- [OpenFeature Documentation](https://openfeature.dev/docs)
- [flagd Documentation](https://flagd.dev/reference/overview/)
- [OpenFeature Python SDK](https://github.com/open-feature/python-sdk)
- [flagd Provider for Python](https://github.com/open-feature/python-sdk-contrib/tree/main/providers/openfeature-provider-flagd)
