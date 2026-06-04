# Feature Flags with Event-Driven Updates

## Overview

JoustMania uses [OpenFeature](https://openfeature.dev/) with [flagd](https://flagd.dev/) for runtime configuration management. Feature flags allow changing game behavior without redeploying code.

**Key Features:**
- **Event-driven updates** - Changes propagate instantly via gRPC streams (no polling)
- **Type-safe evaluation** - Flags are strongly typed (boolean, string, integer, float, object)
- **Observable** - Metrics and logging for flag evaluations and changes
- **Domain-scoped** - One flag file per domain, isolated by `flagSetId` metadata
- **Developer-friendly** - Edit flag files in `services/flagd/` and see changes in <100ms

## Naming Convention

The flag schema follows a strict convention (issue #725):

- **`flagSetId` is THE namespace.** Each flag file declares a single `flagSetId`
  (the domain name) and contains only flags belonging to that domain. The
  filename matches the domain: `system.json` → `flagSetId: "system"`.
- **Bare `snake_case` keys.** A flag's key is just its name within the domain —
  there is no domain prefix on the key. The domain is already expressed by the
  file's `flagSetId`. For example, the controller backend flag is keyed
  `backend` (not `controller_backend`) inside `controller.json`.
- **Dotted sub-keys only for genuine sub-structure.** Use a dot when a flag is
  one leaf of a logically grouped cluster, e.g. `game_loop.update_frequency_hz`,
  `idle.timeout_minutes`, `nonstop.time_limit_seconds`,
  `werewolf.reveal_time_seconds`. A dot is *not* used to namespace by domain.

### Domains

| File | `flagSetId` | Purpose | Primary consumers |
|------|-------------|---------|-------------------|
| `system.json` | `system` | Game loop cadence, idle behavior, sentinel rotation, pairing intervals | game-coordinator, controller-manager |
| `controller.json` | `controller` | Controller backend selection, Bluetooth routing, fault injection | controller-manager |
| `observability.json` | `observability` | Metrics export cadence, profiling, span detail | all services |
| `game.json` | `game` | Game-mode settings (admin-adjustable) | game-coordinator, menu |
| `user.json` | `user` | User preferences (persist across sessions) | menu, audio |
| `agent.json` | `agent` | Autonomous agent control (existence/objective/capability/permission + fitness) | agent, game-coordinator |
| `interventions.json` | `interventions` | Runtime ACT control plane written by the agent | agent (writer), game-coordinator (applier) |
| `rollout.json` | `rollout` | Act 2 progressive backend rollout control | controller-manager |

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────┐
│  Flag Files (services/flagd/) — one file per domain     │
│  - system.json         (flagSetId: "system")            │
│  - controller.json     (flagSetId: "controller")        │
│  - observability.json  (flagSetId: "observability")     │
│  - game.json           (flagSetId: "game")              │
│  - user.json           (flagSetId: "user")              │
│  - agent.json          (flagSetId: "agent")             │
│  - interventions.json  (flagSetId: "interventions")     │
│  - rollout.json        (flagSetId: "rollout")           │
│  - Watched by flagd via inotify (Linux) or polling      │
└──────────────────┬──────────────────────────────────────┘
                   │
                   │ File change detected
                   ▼
┌─────────────────────────────────────────────────────────┐
│  flagd service                                          │
│  - gRPC server implementing OpenFeature Flagd Protocol  │
│  - Loads all flag files via --uri arguments              │
│  - Uses flagSetId metadata for domain scoping           │
│  - Pushes flag updates when any file changes            │
└──────────────────┬──────────────────────────────────────┘
                   │
                   │ gRPC bidirectional stream
                   │ SyncFlags() RPC
                   ▼
┌─────────────────────────────────────────────────────────┐
│  OpenFeature Domain-Scoped Providers                    │
│  - One provider per domain, bound by flagSetId          │
│  - Each provider uses selector="flagSetId=<domain>"     │
│  - Connects to flagd via IN_PROCESS resolver            │
│  - Emits PROVIDER_CONFIGURATION_CHANGED event           │
└──────────────────┬──────────────────────────────────────┘
                   │
                   │ Event callback / Direct reads
                   ▼
┌─────────────────────────────────────────────────────────┐
│  Service Consumers                                      │
│  - RuntimeConfigManager: event-driven cache             │
│    (reads system + controller + game)                   │
│  - Menu: reads game + user; writes game.json/user.json  │
│    via FlagConfigWriter                                 │
│  - Audio: reads user (voice, audio on/off)              │
│  - Agent: reads agent; writes interventions.json        │
│  - Game Coordinator: applies interventions.json         │
└─────────────────────────────────────────────────────────┘
```

**Latency:** Flag file change → Config update = **<100ms**

### Three-Layer Context Merging

Flag evaluation uses a three-layer evaluation context that flagd merges in order
of increasing precedence:

1. **Global context** — process-wide defaults set when the provider is
   initialized (e.g. service name, environment).
2. **Domain/client context** — attributes attached to a specific domain client.
3. **Per-evaluation context** — attributes passed at the call site (e.g. a
   controller `serial`, a `service.name` for per-service targeting).

Targeting rules (fractional rollout, per-serial routing, per-service overrides)
read attributes from this merged context. The most specific layer wins on
conflict.

### How Event-Driven Updates Work

1. **Startup:** Each consumer registers a callback for
   `PROVIDER_CONFIGURATION_CHANGED` events on the domains it cares about.
2. **Normal Operation:** Config is cached and served from memory (zero overhead).
3. **Flag Change:** When any flag file is edited:
   - flagd detects the file change
   - flagd pushes update to all connected clients via gRPC stream
   - the OpenFeature provider for that domain emits
     `PROVIDER_CONFIGURATION_CHANGED`
   - the consumer's callback fires, re-evaluates flags, updates its cache
   - Logs show e.g. `🚩 Feature flags changed: ['game_loop.update_frequency_hz']`
   - Metrics increment `game_flag_configuration_changes_total`

**Why event-driven?**
- **No polling overhead** - Config is read from cache, not from flagd on every game loop iteration
- **Instant updates** - Changes propagate in <100ms instead of waiting for next poll interval
- **Lower CPU usage** - No periodic flag evaluations
- **No rate limiting** - Avoids "too_many_pings" errors from excessive gRPC keepalives

### Thread Safety

Flag change events fire in a background thread. Consumers such as
RuntimeConfigManager use `threading.RLock()` to protect the config cache during
reads/writes.

## Consuming a Domain

Services consume a domain by initializing it once with its `flagSetId` and then
reading flags from the returned client:

```python
from lib.feature_flags import init_flag_domain, get_flag_client

init_flag_domain("game")            # flagSetId == filename == domain
client = get_flag_client("game")
hz = client.get_integer_value("game_loop.update_frequency_hz", 60)
```

- **RuntimeConfigManager** initializes and reads `system`, `controller`, and
  `game`.
- **Menu** reads `game` and `user`, and writes `game.json` / `user.json` via
  `FlagConfigWriter` (atomic writes that flagd hot-reloads).
- **Audio** reads `user`.
- **Agent** reads `agent` and writes `interventions.json`; the **Game
  Coordinator** applies `interventions.json`.

## Available Flags

### System (`services/flagd/system.json`, `flagSetId: "system"`)

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `game_loop.update_frequency_hz` | integer | 15, 30, 60 (default 60) | Game loop update frequency |
| `idle.enabled` | boolean | true, false | Enable idle mode |
| `idle.timeout_minutes` | integer | minutes | Minutes of inactivity before idle |
| `sentinel.count` | integer | count | Number of sentinel controllers |
| `sentinel.rotation_minutes` | integer | minutes | Sentinel rotation interval |
| `pairing.poll_interval` | number | seconds | Controller pairing poll interval |
| `pairing.bt_monitor_interval` | number | seconds | Bluetooth monitor interval |

### Controller (`services/flagd/controller.json`, `flagSetId: "controller"`)

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `backend` | string | `mock`, `python`, `rust`, comma-separated combos (default `python_rust`) | Controller backend selection |
| `poll_drop_threshold` | integer | count | Poll-drop tolerance before action |
| `bluetooth_backend` | string | `python`, `rust`, `unstable` (default `python`) | Per-serial Bluetooth adapter routing (renamed from `controller_adapter_routing`; Act 2 agent rollout target) |
| `chaos_fault_type` | string | `none`, `poll_drop`, `accel_spike`, `led_failure`, `disconnect` | Chaos fault injection for controllers (see [ChaosAdapter](architecture/controller-backends.md#chaosadapter-fault-injection)) |

### Observability (`services/flagd/observability.json`, `flagSetId: "observability"`)

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `metrics_export_interval_ms` | integer | milliseconds | Metrics export cadence (supports per-service targeting via `service.name`) |
| `profiling_enabled` | boolean | true, false | Enable runtime profiling |
| `grpc_rpc_spans` | boolean | true, false | Emit per-RPC gRPC spans |

### Game (`services/flagd/game.json`, `flagSetId: "game"`)

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `sensitivity` | integer | 0-4 | Death detection sensitivity (Werewolf targeting supported) |
| `num_teams` | integer | 2-6 | Number of teams for team modes |
| `random_assignment` | boolean | true, false | Random team assignment |
| `force_all_start` | boolean | true, false | Force start with all connected controllers |
| `countdown_phase_duration_ms` | integer | milliseconds | Countdown phase duration |
| `winner_rainbow_duration_ms` | integer | milliseconds | Winner rainbow LED duration |
| `invincibility_seconds` | float | 2.0-8.0 | Invincibility duration (Tournament, FightClub) |
| `nonstop.time_limit_seconds` | integer | 0, 60-300 | Nonstop Joust time limit (0 = unlimited) |
| `fight_club.min_rounds` | integer | 5-20 | Minimum rounds for Fight Club |
| `werewolf.reveal_time_seconds` | float | 20.0-60.0 | Werewolf reveal time |

### User (`services/flagd/user.json`, `flagSetId: "user"`)

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `menu_voice` | string | aaron, ivy | Voice actor for announcements |
| `play_audio` | boolean | true, false | Enable/disable audio output |
| `current_game` | string | game mode names | Currently selected game mode |
| `game_instructions` | boolean | true, false | Show game instructions |
| `menu_auto_start` | boolean | true, false | Auto-start menu on boot |

### Agent (`services/flagd/agent.json`, `flagSetId: "agent"`)

The agent domain is organized into **four flag layers** (issue #725 acceptance
criterion), plus a fitness configuration block used to score agent behavior.
The layers form a deliberate gate sequence: *existence* decides whether the
agent runs at all; *objective* shapes what it optimizes for; *capability*
constrains how it reasons; *permission* bounds what it is allowed to do.

#### Layer 1 — Existence

Controls whether the autonomous agent runs.

| Flag | Type | Values | Default | Description |
|------|------|--------|---------|-------------|
| `enabled` | boolean | true, false | false (off) | Master switch for the agent |
| `mode` | string | `rules`, `llm` | — | Decision engine: deterministic rules vs. LLM |

#### Layer 2 — Objective

Selects a weight preset over the optimization axes `{endurance, balanced,
accelerate, chaos}`.

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `objectives` | object | preset names `endurance_focused`, `balanced_focused`, `accelerate_focused`, `chaos_focused`, `mixed` | Weight preset over `{endurance, balanced, accelerate, chaos}` |

#### Layer 3 — Capability

Constrains the agent's reasoning model and prompt style.

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `model` | string | `phi4-mini`, `gemma3:4b`, `claude`, `copilot` | Model backing the agent |
| `prompt_variant` | string | `conservative`, `aggressive`, `balanced` | Prompt style |

#### Layer 4 — Permission

Bounds what the agent is allowed to do at runtime. The `interventions_allowed`
variants are populated from the #722 research — see
[722-intervention-surface.md §6](research/722-intervention-surface.md).

| Flag | Type | Values | Default | Description |
|------|------|--------|---------|-------------|
| `interventions_allowed` | string | `none`, `ambient`, `standard`, `full` | `ambient` | Permitted intervention surface (from #722 §6) |
| `policy.battery_threshold` | integer | percent | 20 | Battery floor before agent acts |
| `policy.movement_variance_window` | integer | samples | 10 | Window for movement-variance checks |
| `policy.max_interventions_per_minute` | integer | count | 2 | Rate cap on interventions |

#### Fitness configuration

Thresholds the agent uses to evaluate whether its objective is being met.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `fitness.endurance.min_session_seconds` | integer | 120 | Minimum session length for endurance objective |
| `fitness.balanced.max_skill_gap` | float | 0.4 | Maximum acceptable skill gap |
| `fitness.balanced.spike_survival_threshold` | float | 0.8 | Spike-survival fitness threshold |
| `fitness.accelerate.target_session_seconds` | integer | 60 | Target session length for accelerate objective |
| `fitness.bluetooth.max_event_gap_ms` | integer | 50 | Max acceptable Bluetooth event gap |
| `fitness.bluetooth.max_dropped_events_pct` | float | 0.02 | Max acceptable dropped-event fraction |
| `fitness.bluetooth.min_movement_update_hz` | integer | 10 | Min acceptable movement update rate |

### Interventions (`services/flagd/interventions.json`, `flagSetId: "interventions"`)

The interventions domain is the **runtime ACT control plane**: the agent
*writes* these flags and the game coordinator *applies* them. All defaults are
inert, so an idle or absent agent has no effect. See
[722-intervention-surface.md §8](research/722-intervention-surface.md) for the
design rationale behind this control plane.

**State-shaped** (continuously applied while set):

| Flag | Type | Targeting | Description |
|------|------|-----------|-------------|
| `music_tempo_override` | object/number | — | Override music tempo |
| `global_sensitivity_override` | object/number | — | Override death-detection sensitivity globally |
| `player_sensitivity_factor` | object/number | per-serial | Per-player sensitivity multiplier |
| `shield_seconds` | object/number | per-serial | Grant per-player shield duration |
| `volume_override` | object/number | — | Override audio volume |

**Edge-triggered** (apply once per change; carry a nonce to de-duplicate):

| Flag | Type | Description |
|------|------|-------------|
| `eliminate_player` | object (nonce'd) | Eliminate a target player |
| `revive_player` | object (nonce'd) | Revive a target player |
| `audio_cue` | object (nonce'd) | Play a one-shot audio cue |
| `controller_effect` | object (nonce'd) | Trigger a one-shot controller effect |
| `end_game` | object (nonce'd) | End the current game |

### Rollout (`services/flagd/rollout.json`, `flagSetId: "rollout"`)

Act 2 progressive backend rollout control.

| Flag | Type | Values | Description |
|------|------|--------|-------------|
| `target_backend` | string | backend name | Backend to migrate controllers toward |
| `strategy` | string | `progressive`, `immediate`, `off` | Rollout strategy |
| `current_controller_count` | integer | count | Controllers currently on the target backend |
| `remediation_allowed` | boolean | true, false | Allow automatic rollback/remediation |

### Adding New Flags

1. Pick the domain (file) the flag belongs to and add it to that file only —
   the file's `flagSetId` is its namespace.
2. Name the key in bare `snake_case`. Use a dotted sub-key only if it is one
   leaf of a genuine sub-structure (e.g. `idle.timeout_minutes`), never to
   re-encode the domain.
3. Add the flag definition with `"state": "ENABLED"` and variants.
4. In your service code, initialize the domain and read the flag:
   ```python
   from lib.feature_flags import init_flag_domain, get_flag_client

   init_flag_domain("game")
   client = get_flag_client("game")
   value = client.get_integer_value("my_new_flag", default_value)
   ```
5. No restart required — flagd detects the file change automatically.

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

```bash
docker compose logs -f game-coordinator | grep -E "🎯|🚩|Flag"
```

You should see on startup:
```
INFO - Feature flag client initialized
INFO - Registered PROVIDER_CONFIGURATION_CHANGED event handler
```

### 3. Edit Flag Values

Open the flags file for the domain you want to change:

```bash
# On Raspberry Pi
nano services/flagd/system.json
```

Change a flag value (e.g., `game_loop.update_frequency_hz` from `low` to `high`):

```json
{
  "flags": {
    "game_loop.update_frequency_hz": {
      "state": "ENABLED",
      "variants": {
        "low": 15,
        "medium": 30,
        "high": 60
      },
      "defaultVariant": "high"
    }
  }
}
```

Save the file (Ctrl+O, Enter, Ctrl+X).

### 4. Verify Event Detection

Within **1 second**, you should see in the logs:

```
🚩 Feature flags changed: ['game_loop.update_frequency_hz']
🎯 Config updated: update_frequency_hz 15 → 60 Hz
```

### 5. Test During Gameplay

1. Start a game with 2+ controllers
2. In another terminal, change `game_loop.update_frequency_hz` to `low` (15 Hz)
3. Observe gameplay becomes less responsive
4. Change back to `high` (60 Hz)
5. Observe gameplay becomes more responsive

The game adapts in real-time without restarting.

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

Or view in Grafana's **Feature Flags** dashboard.

## Metrics

The following metrics are exported to Prometheus:

### `game_flag_evaluations_total{flag_key}`

**Type:** Counter
**Labels:** `flag_key` (e.g., "game_loop.update_frequency_hz")
**Description:** Total number of times each flag has been evaluated

```promql
# Evaluation rate per flag
rate(game_flag_evaluations_total[1m])

# Most frequently evaluated flags
topk(5, sum by (flag_key) (game_flag_evaluations_total))
```

### `game_flag_configuration_changes_total`

**Type:** Counter
**Description:** Total number of PROVIDER_CONFIGURATION_CHANGED events received

```promql
increase(game_flag_configuration_changes_total[1h])
rate(game_flag_configuration_changes_total[5m]) > 2
```

### `game_current_update_frequency_hz`

**Type:** Gauge
**Description:** Current configured update frequency in Hz (15, 30, or 60)

```promql
game_current_update_frequency_hz
game_current_update_frequency_hz < 30
```

## Troubleshooting

### No flag change events detected

**Symptoms:** Edit a flag file but no `🚩 Feature flags changed` log appears

```bash
docker compose ps flagd
docker compose logs flagd --tail=50
docker compose exec flagd ls -l /etc/flagd/
```

**Solutions:**
- Ensure flagd container is healthy
- Verify flag files are mounted correctly in docker-compose.yml
- Check file permissions (must be readable by flagd)

### "Could not initialize feature flags" error

```bash
docker compose exec game-coordinator pip list | grep openfeature
```

**Solutions:**
- Rebuild image: `docker compose up -d --build game-coordinator`
- Verify `pyproject.toml` includes `openfeature-sdk` and `openfeature-provider-flagd`

### "too_many_pings" errors (HTTP 429)

This should NOT happen with event-driven updates. If you see this:

```bash
docker compose logs game-coordinator | grep "Registered PROVIDER_CONFIGURATION_CHANGED"
```

**Solutions:**
- Verify the consumer registers the event handler on startup
- Check that `get_config()` returns cached values, not re-evaluating flags

### Metrics not showing up in Prometheus

```bash
curl -s localhost:9090/api/v1/label/__name__/values | jq -r '.data[]' | grep game_flag
docker compose logs otel-collector | grep game-coordinator
```

**Solutions:**
- Wait for the metrics export interval
- Verify Prometheus is scraping the service (check Targets page)
- Check OTEL_EXPORTER_OTLP_ENDPOINT is set correctly

### Flag changes don't affect gameplay

```bash
docker compose logs game-coordinator | tail -20
# Should see "🎯 Config updated" logs
```

**Solutions:**
- Ensure the flag key matches exactly in code and JSON (including any dotted sub-key)
- Verify you edited the correct domain file (the `flagSetId` must match the consumer)
- Verify the config value is actually being used in game logic

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
    init_flag_domain("system")
    self.flags = get_flag_client("system")
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
DEBUG - Evaluating flag: game_loop.update_frequency_hz
DEBUG - Flag value: 60
```

## References

- [OpenFeature Documentation](https://openfeature.dev/docs)
- [flagd Documentation](https://flagd.dev/reference/overview/)
- [OpenFeature Python SDK](https://github.com/open-feature/python-sdk)
- [flagd Provider for Python](https://github.com/open-feature/python-sdk-contrib/tree/main/providers/openfeature-provider-flagd)
- [Intervention surface research (#722)](research/722-intervention-surface.md)
