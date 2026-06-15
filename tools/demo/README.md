# Agent demo driver (#794)

Scripted mock gameplay that **trips the agent's decision rules on cue**, so the
agentic stack can be exercised **repeatably** — both as a verification tool and as
the on-stage demo driver. It plays deterministic mock-controller movement patterns
against a **running** stack so the agent's live observe → decide → (act) pipeline
reacts visibly.

This is a **driver/tool**, not a service: a standalone script invoked against the
running stack. It changes no agent/coordinator behavior.

## How it works

The agent does **not** receive gameplay over a side channel. It OBSERVES a live
game through OTLP telemetry — the game coordinator emits per-player metrics
(`game_player_skill_level`, `game_player_movement_variance`,
`game_player_movement_intensity`, `game_duration_seconds`, `game_player_alive`, …)
which the agent's `GameContext` store ingests, then runs its objective-weighted
**rules engine** (`services/agent/decision/ruleset.go`) each cycle.

So to trip a rule on cue, the driver makes the coordinator emit the right metric
values by playing movement patterns. The coordinator classifies each frame's accel
magnitude into a zone (`services/game_coordinator/games/analytics.py`):

| Zone | Magnitude | Driver vector |
|------|-----------|---------------|
| STILL | < 1.1g | `ZONE_STILL` (~1.0g) |
| ACTIVE | 1.1–1.5g | `ZONE_ACTIVE` (~1.3g) |
| WARNING | 1.5–2.0g | `ZONE_WARNING` (~1.7g) |
| DANGER | > 2.0g | `ZONE_DANGER` (~2.3g) |

`SimulateMovement` sets a **persistent** accel vector, so a "beat" just sets each
controller's magnitude. From the zone history + a rolling variance window the
coordinator derives `skill_level`, `movement_variance`, `movement_intensity`,
which the rules read.

It drives a **REAL (primary, menu-started) game** — same path as the dashboard
start button (StartMenu → `select_game` web command → TRIGGER-ready → auto-start)
— **not** a shadow/experiment game, so the agent's live decision/intervention path
reacts.

## Scenario → expected rule → expected observable

| `--scenario` | Movement pattern | Trips | Observable |
|--------------|------------------|-------|------------|
| `skill_gap` | high-skill cohort steady ACTIVE; low-skill cohort erratic STILL↔DANGER | **R3** skill gap > `fitness.balanced.max_skill_gap` (0.4) | `decision.action=adjust_player_sensitivity` on the outlier (a *difficulty* intervention — **blocked** under the stock `ambient` allow-list) |
| `statue` | one controller held perfectly still; others jostle | **R8** variance < `statueVarianceEpsilon` (0.05) | `decision.action=send_controller_effect` — **dispatches** under stock `ambient` |
| `low_variance` | whole field near-still | **R1/R2/R8** endurance + statue pressure | calming cue / slow-tempo decisions; statue nudges |
| `death_cascade` | eliminate players fast while the session is young | **R1/R2** endurance (session at risk) | `decision.action=adjust_music_tempo` / `play_audio_cue` |
| `idle_dominator` | one player pegged high, the rest near-still | **R6** eliminate-least-active (past the accelerate target) | activity spread; eliminate-player shape once past `fitness.accelerate.target_session_seconds` (60s) |
| `blocked_battery` | skill-gap pattern under `ambient` | **R3 blocked by the permission layer** | `decision.blocked=true block_reason=not_allowed`; `game_interventions_total{blocked="true"}` — the permission chain shown end to end |

> **Why a blocked beat matters.** `skill_gap`/`blocked_battery` produce a
> *difficulty* intervention (`adjust_player_sensitivity`), which the stock
> `interventions_allowed=ambient` allow-list does **not** permit. That makes the
> **permission chain visible on the dashboards** without anything reaching the game
> — one of the acceptance criteria. To let it *through* instead, widen
> `interventions_allowed` to `standard`/`full` in `services/flagd/agent.json`
> (see the ACT runbook).

## Running it

The driver needs `grpcio` + `protobuf`. The simplest reproducible way is the
game-coordinator's uv environment (it already vendors both and builds the proto
package):

```bash
# 1. Bring the stack up (mock controllers + agent + observability).
make dry-run
./scripts/agent-killswitch.sh on    # the agent loop is inert until you do this

# (Optional) to see the agent ACT, not just decide — see docs/agent-act-runbook.md:
#   AGENT_INTERVENTIONS_ENABLED=true on the agent container (restart), and
#   widen interventions_allowed if you want a difficulty intervention to dispatch.

# 2. Fire a scenario.
cd services/game_coordinator
uv run python ../../tools/demo/demo_driver.py --scenario statue --players 4 --duration 30

# List scenarios:
uv run python ../../tools/demo/demo_driver.py --list

# Loop a beat for the talk (Ctrl-C to stop):
uv run python ../../tools/demo/demo_driver.py --scenario skill_gap --loop
```

Or, with `grpcio`+`protobuf` on your PATH and the repo root importable, run
`python3 tools/demo/demo_driver.py …` directly.

### Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--scenario` | `skill_gap` | scenario to play (see `--list`) |
| `--players` | `4` | number of mock controllers |
| `--duration` | `30` | seconds to drive the pattern |
| `--game-mode` | `JoustFFA` | game mode to start |
| `--loop` | off | replay until interrupted |
| `--host` / `--mock-port` / `--menu-port` / `--coord-port` | `localhost` / `50062` / `50054` / `50053` | stack endpoints |
| `--keep-game` / `--keep-controllers` | off | skip the end-of-run cleanup |

## Where to watch the agent react

| Tool | URL | What |
|------|-----|------|
| agent logs | `docker compose logs -f agent` | `agent.signal_received → agent.decision (→ agent.action)` |
| Jaeger | `http://localhost:8080/jaeger/` (service `agent`) | the decision audit trace + span attrs `decision.action`, `decision.objective_served`, `decision.reason`, `decision.blocked`/`decision.block_reason` |
| Prometheus | `http://localhost:8080/prometheus/` | `agent_evaluations_total` (#1053), `game_interventions_total{type,objective,blocked}` |
| Grafana | `http://localhost:8080/grafana/` | agent / experiments dashboards |

**Even with the act gates closed**, the OBSERVE/DECIDE pipeline still emits
`agent.signal_received → agent.decision` spans and `agent_evaluations_total` — that
is enough to confirm the driver tripped a rule. LLM inference being unreachable
(#1059) is fine: the decision loop runs in **rules mode**, exactly what this driver
targets.

## See also

- `docs/agent-act-runbook.md` — opening the act gates / blocked-decision demos
- `docs/agent-dry-run-runbook.md` — bringing the stack up
- `services/agent/decision/ruleset.go` — the rules R1–R9 this driver targets
- `services/game_coordinator/games/analytics.py` — zone → skill/variance derivation
