# A/B Testing with Feature Flags

**Part of Issue #21**: Feature Flags & Dynamic Configuration

## Overview

JoustMania supports A/B testing through percentage-based flag rollouts powered by flagd. This allows you to:

- Test new features with a subset of players
- Gradually roll out changes (e.g., 10% → 25% → 50% → 100%)
- Compare performance between control and treatment groups
- Make data-driven decisions about game balance

## How It Works

### 1. Flag Configuration

Define experiment flags in `services/flagd/flags.json` with percentage-based targeting:

```json
{
  "flags": {
    "experiment_high_frequency_rollout": {
      "state": "ENABLED",
      "variants": {
        "control": false,
        "treatment": true
      },
      "defaultVariant": "control",
      "targeting": [
        {
          "variant": "treatment",
          "percentage": 10
        },
        {
          "variant": "control",
          "percentage": 90
        }
      ]
    }
  }
}
```

This configuration:
- Assigns 10% of evaluations to "treatment" variant
- Assigns 90% of evaluations to "control" variant
- Uses consistent hashing (same player always gets same variant)

### 2. Experiment Tracking

Use the `lib/experiments.py` module to track assignments:

```python
from lib.feature_flags import get_feature_flag_client
from lib.experiments import evaluate_experiment_flag
from lib.player_context import build_player_context

# Get flag client
client = get_feature_flag_client()

# Build player context for consistent assignment
context = build_player_context(
    player=player,
    game_mode="FFA",
    controller_count=4,
    game_duration_seconds=game_duration,
)

# Evaluate experiment flag
enabled, variant = evaluate_experiment_flag(
    client,
    "experiment_high_frequency_rollout",
    False,  # default value
    context,
)

# Use result
if enabled:
    update_frequency = 60  # treatment
else:
    update_frequency = 30  # control
```

The `evaluate_experiment_flag` function automatically:
- Evaluates the flag with player context
- Tracks assignment in Prometheus metrics
- Logs the assignment for debugging
- Returns both the value and variant name

### 3. Metrics & Analysis

Experiment assignments are tracked in Prometheus:

**Counters:**
```
game_experiment_variant_assigned{experiment="high_frequency_rollout",variant="treatment"} 42
game_experiment_variant_assigned{experiment="high_frequency_rollout",variant="control"} 378
```

**Gauges:**
```
game_experiment_active_participants{experiment="high_frequency_rollout"} 12
```

View in Grafana:
```promql
# Assignment distribution
rate(game_experiment_variant_assigned[5m])

# Active participants
game_experiment_active_participants

# Win rate by variant (cross-reference with game_wins_total)
sum by (variant) (game_wins_total)
  * on (serial) group_left(variant)
  game_experiment_variant_assigned
```

## Example Experiments

### 1. High Frequency Rollout (10% treatment)

**Goal:** Test if 60Hz update frequency improves gameplay

```json
"experiment_high_frequency_rollout": {
  "state": "ENABLED",
  "variants": {
    "control": false,
    "treatment": true
  },
  "defaultVariant": "control",
  "targeting": [
    {"variant": "treatment", "percentage": 10},
    {"variant": "control", "percentage": 90}
  ]
}
```

**Metrics to compare:**
- `game_loop_frame_consistency_percent` (should be higher for treatment)
- `game_loop_jitter_ms` (should be lower for treatment)
- `player_deaths_total` (are reactions better?)

### 2. Aggressive Thresholds (3-way split)

**Goal:** Find optimal death threshold adjustments

```json
"experiment_aggressive_thresholds": {
  "state": "ENABLED",
  "variants": {
    "control": 0.0,
    "treatment_mild": -0.1,
    "treatment_aggressive": -0.2
  },
  "defaultVariant": "control",
  "targeting": [
    {"variant": "treatment_aggressive", "percentage": 10},
    {"variant": "treatment_mild", "percentage": 20},
    {"variant": "control", "percentage": 70}
  ]
}
```

**Metrics to compare:**
- `game_duration_seconds` (are games faster?)
- `player_deaths_total` (is there more action?)
- `player_warnings_total` (are players more cautious?)

### 3. Grace Period Duration (balanced 3-way)

**Goal:** Test different respawn protection durations

```json
"experiment_grace_period_duration": {
  "state": "ENABLED",
  "variants": {
    "control": 0.5,
    "short": 0.3,
    "long": 0.8
  },
  "defaultVariant": "control",
  "targeting": [
    {"variant": "short", "percentage": 25},
    {"variant": "long", "percentage": 25},
    {"variant": "control", "percentage": 50}
  ]
}
```

**Metrics to compare:**
- `player_respawns_total` (in Nonstop Joust mode)
- Time between death and next death (custom metric)

## Best Practices

### 1. Consistent Player Assignment

Always use player context for flag evaluation to ensure:
- Same player always gets same variant (consistency)
- Player experience doesn't change mid-game
- Results are comparable across sessions

```python
# Good: consistent assignment
context = build_player_context(player, game_mode, controller_count, duration)
value = client.get_boolean_value("experiment_flag", False, context)

# Bad: random assignment each time
value = client.get_boolean_value("experiment_flag", False)
```

### 2. Start Small, Scale Up

Gradual rollout strategy:
1. **10%** - Initial test, verify metrics
2. **25%** - Expand if positive results
3. **50%** - Majority test
4. **100%** - Full rollout (or update default)

### 3. Run Experiments for Multiple Sessions

Don't decide based on a single game:
- Run for at least 10-20 games
- Collect data across different player counts
- Account for time-of-day variations

### 4. Monitor Both Success and Safety Metrics

**Success metrics** (what you want to improve):
- Game duration
- Player engagement
- Win distribution

**Safety metrics** (what you don't want to break):
- Frame consistency
- Error rates
- Player warnings (too many = frustration)

## Propagation Timing

Flag changes propagate within **10 seconds** (verified by `scripts/test_flag_propagation.py`):

```bash
# Test propagation timing
python scripts/test_flag_propagation.py
```

This means:
- Change flags.json
- Wait up to 10 seconds
- New values are active

For immediate changes during development:
```bash
# Restart flagd container
docker compose restart flagd
```

## Disabling Experiments

To disable an experiment:

```json
"experiment_name": {
  "state": "DISABLED",  // Change from ENABLED
  // ... rest of config
}
```

Or remove the flag entirely and deploy default behavior.

## Integration with Adaptive Rewards

Experiments work alongside adaptive rewards (Phase 52):

```python
# Experiment determines base setting
high_freq_enabled, variant = evaluate_experiment_flag(
    client,
    "experiment_high_frequency_rollout",
    False,
    context,
)

update_frequency = 60 if high_freq_enabled else 30

# Adaptive rewards then adjust per-player thresholds
adjustment = client.get_float_value(
    "ffa_death_threshold_adjustment",
    0.0,
    context,  # Uses win_rate, K/D, warnings
)
```

This allows combining:
- **A/B testing**: System-wide experiments
- **Adaptive rewards**: Per-player personalization

## Further Reading

- [flagd Documentation](https://flagd.dev/)
- [OpenFeature Specification](https://openfeature.dev/)
- [JSONLogic for Targeting](https://jsonlogic.com/)
