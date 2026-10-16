# Telemetry

> Technical spec. Part of the [spec tree](../README.md). State: `main` @ 81c9ecea (2026-10-16, includes PRs #1256, #1257, #1259, #1261, #1265).
> Telemetry is the agent's only view of the game (C-001), so span names, attribute keys, metric names and the
> log lines below are a **contract**: the agent, the dashboard SPA, Grafana, alerts and integration tests match
> on them. Change a name here first.

## 1. Pipeline

| Stage | What | Source |
|---|---|---|
| Producers | Python services: OTLP traces (`OTLPSpanExporter`), OTLP metrics (`OTLPMetricExporter`, interval from `metrics_export_interval_ms`), OTLP logs with trace ids; Go agent / connect-proxy and rust-hid: OTLP, metrics every 1 s | `lib/telemetry.py`, `lib/otel_metrics.py:85-154`, `lib/otel_logging.py`, `services/agent/otel.go:133` |
| Collector receivers | `otlp` (4317 / 4318), `prometheus/observability`, `prometheus/infrastructure` (flagd 8014, envoy 9901), `filelog/docker` (all container logs), `hostmetrics`, `docker_stats` | `services/otel-collector/config.yaml:2-153` |
| Processors | `memory_limiter` (600 MiB, spike 150 MiB, env-overridable), batch (traces 2 s, metrics 100 ms, logs 5 s), resource attributes; **no sampling** | `config.yaml:160-309` |
| Exporters | Jaeger (OTLP), VictoriaMetrics (`prometheusremotewrite`), Prometheus exporter :8889, Loki (OTLP HTTP), **agent** (`otlp/agent`, 2 s timeout, no retries, queue 1000) | `config.yaml:317-355` |
| Pipelines | traces → Jaeger + agent; OTLP metrics → VictoriaMetrics + Prometheus exporter + agent; other metrics → VictoriaMetrics; logs → Loki (agent gets no logs) | `config.yaml:358-406` |
| Prometheus | scrapes the collector every 500 ms (alert rules), controller-manager `:8000`, flagd; global 10 s | `services/prometheus/prometheus.yml` |
| Retention | VictoriaMetrics 7 d, Prometheus 30 d, Jaeger bounded badger TTL | `docker-compose.yml:507,539`; `services/jaeger/config.yaml` |

Label mapping: `prometheusremotewrite` maps `service.name` to `job` prefixed with `service.namespace`
(VictoriaMetrics: `job="joustmania/game-coordinator-service"`); Prometheus strips the prefix
(`job="game-coordinator-service"`).

Service identities (`OTEL_SERVICE_NAME` / namespace): `controller-manager-service`, `pairing-daemon`,
`rust-hid`, `python-hid`, `connect-proxy`, `agent` (namespace `infrastructure`); `game-coordinator-service`,
`menu-service`, `audio-service` (namespace `joustmania`) (`docker-compose.yml`).

**Traceability:** C-001, FR-040, FR-080 to FR-082, NFR-021 to NFR-024 / UC-035, UC-041 / `services/otel-collector/config.yaml`, `lib/telemetry.py`, `lib/otel_metrics.py` / `tests/integration/test_decide_backend_e2e.py` (Jaeger relay).

## 2. Conventions

### 2.1 Attribute namespaces

| Namespace | Keys | Producer |
|---|---|---|
| `game.*` | `game.id`, `game.kind`, `game.name`, `game.mode`, `game.trace_id`, `game.trace_span_id`, `game.winner`, `game.duration_seconds` | game-coordinator, agent |
| `player.*` | `player.serial`, `player.team`, `player.color`, `player.name`, `player.count` | game-coordinator; agent (`player.serial`, `player.name` on targeted `agent.decision` / `agent.action`, #1295) |
| `controller.*`, `bluetooth.*` | `controller.serial`, `controller.adapter`, `bluetooth.event_gap_ms`, `bluetooth.dropped_events_pct`, `bluetooth.movement_update_hz`, `bluetooth.target_backend`, `bluetooth.rollout_count`, `bluetooth.rollout_strategy` | controller-manager |
| `experiment.*`, `verdict.*` | `experiment.id`, `experiment.arm`, `experiment.objective`, `experiment.flag_key`, `experiment.target_n`, `experiment.thesis`; `verdict.outcome`, `verdict.delta`, `verdict.significant` | game-coordinator, agent |
| `agent.*`, `decision.*`, `decide.*`, `intervention.*`, `policy.*`, `inference.*`, `llm.*` | see §4 | agent |
| `gen_ai.*` | OTel GenAI semantic conventions, see §4.2 | agent |
| `rpc.*`, `server.*` | OTel RPC semantic conventions on gRPC spans | all |

### 2.2 Correlation ids

| Id | On spans | On metrics | Notes |
|---|---|---|---|
| game id | `game.id` (agent also `session.id`) | label `game_id` | `game_<12 hex>`; the agent partitions all signals by it |
| game kind | coordinator: `game.kind` = `primary` / `shadow`; agent: `game.kind` = `real` / `shadow` | coordinator label `game_kind` = `primary` / `shadow` | **Two vocabularies under one key** (#1249). Flags use `real` / `shadow`; the agent normalises `primary → real` at ingest (`services/agent/gamecontext/gamekind.go`). |
| experiment, arm | `experiment.id`, `experiment.arm` | `experiment_id`, `arm` on per-game gauges only (never on cumulative counters) | |
| trace context | `game.trace_id`, `game.trace_span_id`, Links | gauge `game_trace_correlation{game_trace_id, game_trace_span_id, gameplay_phase_span_id}` = 1 while live | metric-carried because observable gauges cannot carry exemplars (`services/game_coordinator/metrics.py:44-85`) |
| serial | `player.serial`, `controller.serial` | `serial` | `player.serial` is ONE key on both sides (#1295): the coordinator's player spans and intervention events, and the agent's targeted `agent.decision` / `agent.action`, so one Jaeger search `player.serial=<serial>` finds both |
| intervention | `intervention.id`, `decision.intervention_id` | | joins decision and effect |
| W3C propagation | menu events carry `_traceparent` / `_tracestate`; the agent injects traceparent into shadow-game spawns and model calls | | |

**Traceability:** FR-041, FR-043 / UC-035, UC-041 / `lib/feature_flags.py:63-64`, `services/game_coordinator/game_session.py:55-56,283-292`, `services/agent/gamecontext/` / `services/agent/gamecontext/*_test.go`.

## 3. Spans

### 3.1 gRPC (all Python services, rust-hid)

| Span | Kind | Gate | Source |
|---|---|---|---|
| `{Service}/{Method}` (e.g. `GameCoordinatorService/ForceEndGame`) | CLIENT / SERVER | flag `grpc_rpc_spans` (default enabled), per RPC | `lib/grpc_tracing.py` |
| `grpc.server` (rust-hid) | SERVER | same flag | `services/rust-hid/src/grpc_tracing.rs:105-115` |
| renamed: `StreamButtonEvents`, `StreamGameplayData` (CM); `PlaySound:{sound}`, `PlayMusic:{dir}`, `ChangeTempo:{x}x`, `SetVolume:{pct}` (audio) | SERVER | | `services/controller_manager/servicer.py:241,452`; `services/audio/servicer.py:662-723` |

connect-proxy: `otelhttp` server spans (`/health` filtered) and `otelgrpc` client spans only.

### 3.2 game-coordinator

| Span | Notes | Source |
|---|---|---|
| `<mode display name>` (e.g. `Joust Free-for-All`, `Non Stop Joust`) | **game root**, own trace (#1157); child of `agent.experiment` for experiment shadow games (#1182); attrs `game.name`, `game.id`, `game.kind`, `experiment.id`, `experiment.arm`, `player.count` | `game_session.py:319-380` |
| `initialization_phase`, `<phase>` (`color_assignment`, `team_formation_phase`, `traitor_signal`, `werewolf_intro`, `zombie_intro`, `fight_club_intro`, `tournament_intro`), `gameplay_phase`, `players`, `game_cycle`, `teardown_phase` | phases; `gameplay_phase` is the remote parent of in-game agent spans (#1195) | `games/base.py:2629-2790` |
| `player_lifecycle`, renamed `player: {name}` on the first frame | one per player; attrs `player.serial`, `player.name` (set with the rename); events `death_warning`, `player_death`, `shield_granted`, `player_revived`, `partial_shield_armed`, `shield_feedback` (attrs `shield.kind` = full / partial, `shield.duration_ms`, `shield.sound` = `shield_up` or `none` on a silent extension; UC-040 A16), `soft_penalty_tighten`, `auto_rubberband_boost`, `sensitivity_factor_applied`, `handicap_factor_applied` (#1295; recorded only when the player's factor changes), `player_disconnected`. The events that record an applied intervention (`shield_granted`, `partial_shield_armed`, `soft_penalty_tighten`, `auto_rubberband_boost`, `player_revived`, `sensitivity_factor_applied`, `handicap_factor_applied`, and the base kill path's `player_death`, which carries `death.reason=agent_intervention` for `eliminate_player`) carry `player.serial` and, once the controller reported one, `player.name` (#1295). The span ends at game end, so these events reach Jaeger then (UC-041 BR-001). | `games/base.py` (`player_identity_attributes`), `difficulty_handlers.py` |
| `countdown_phase` | per player (base modes) | `games/base.py:2737` |
| `team_lifecycle` | team modes | `games/teams_base.py:151` |
| `zombie_game`, `werewolf_game`, `fight_club_game`, `tournament_game`, `tournament_match` | modes with their own `run()` | mode files |
| `music_tempo_change`, `music_tempo_ramp` | attrs `old_tempo`, `new_tempo`, `dead_count` | `games/base.py:3184,3235` |
| `controller_poll_degraded`, `controller_led_degraded` | per player | `games/base.py:1737,1768` |

**`player_death` span event: exactly one per death (#1327).** Every death that reaches the base kill path (`BaseGameMode._kill_player`) records exactly one `player_death` event on that player's lifecycle span, in every mode. Before #1327 FFA, Teams / Random Teams, Traitor, Werewolf and NonStop also emitted a second, poorer `player_death` from their `_kill_player_impl`, so each death showed twice in Jaeger (dry run #2). The one event carries:

- always: `player.serial`, `player.name` (once the controller reported one), `death.reason` (`motion` for a threshold death, `agent_intervention` for `eliminate_player`), `accel_magnitude` (0.0 for a non-motion death), `alive_remaining` (alive players after this death), `sensitivity`, `sensitivity_factor`, `music_speed`;
- mode extras, added through `_player_death_event_attributes()` and computed as if the player were already out: Teams / Random Teams `team`, `team_eliminated`; Traitor `team_eliminated` (secret team), `is_traitor`, `visible_team`, `secret_team`; Werewolf `is_werewolf`, `was_revealed`; NonStop `kills`, `deaths` (including this death), `score`.

A mode's `_kill_player_impl` never adds its own `player_death` span event. Mode-specific outcome events stay separate (`zombie_killed`, `team_swap_out`, `match_death`, `round_lost`). This span event is a different channel from the event-bus `player_death` in §5. Consumers that count or order eliminations from spans (the agent's span ingest, §4.4) can count one event as one death.

### 3.3 controller-manager, menu, pairing daemon

| Service | Spans | Source |
|---|---|---|
| controller-manager | `controller.discover`, `controller_connected`, `spawn_controller_process`, `controller.bluetooth_health` (~1 Hz, event `bluetooth_controller_sample`; **consumed by the agent infra loop**), `effect_player_warning`, `effect_player_death`, `effect_winner_rainbow`, `effect_countdown`, `controller.shield_effect` (one per shield layer run on a controller; attrs `controller.serial`, `shield.kind` and `effect.duration_ms` of the first layer; events `shield_blink` (`shield.kind`), `shield_end` with `shield.end_reason` = expired / cancelled; UC-040 A16, A17) | `services/controller_manager/discovery_loop.py:356-767`, `feedback_manager.py:423-809` |
| menu | `menu_publish:{event_type}`, `start_game`, `stop_lobby_music`, `stop_button_monitor`, `restart_button_monitor`, `reset_lobby_state`, `restart_lobby_music`, `admin_mode_session`, `enter_admin_mode`, `exit_admin_mode`, `admin_*` (game_mode, force_start, sensitivity, battery_display, instructions, cycle_option, increase_value, decrease_value, agent_control) | `services/menu/servicer.py`, `handlers/admin.py` |
| pairing-daemon | `bluetooth_monitor_cycle`, `pair_controller`, `process_controller`, `poll_cycle` | `services/pairing_daemon/psmove_pairing/` |

**Traceability:** FR-043, FR-074, FR-080 / UC-020 to UC-030, UC-035, UC-050 / files above / `tests/integration/test_decide_backend_e2e.py`.

## 4. Agent

### 4.1 Spans and topology

| Span | Kind / parent | Source |
|---|---|---|
| `agent.signal_received` | SERVER; remote child of the game's `gameplay_phase` (fallback: game root, else own root); attrs `otlp.signal`, `game.id`, `game.trace_id` | `services/agent/decision/decision.go:585-620`; constant `decision/telemetry.go:34` |
| `agent.decide` | rules path: sibling of `agent.decision` under `agent.signal_received`; remote path: remote child of `gameplay_phase`, Link `link.kind=fire_cycle`, parent of `chat {model}` and `agent.decision` | `decision/decide_hook.go:44-127,622-706` |
| `agent.decision` → `agent.action` | Link to the game trace; event `feature_flag`. A player-targeted decision (blocked or applied) carries `player.serial` (the target) and, when the agent knows it and the game is not `shadow`, `player.name` on both spans (#1295) | `decision/decision.go` (`runDecision`, `targetAttributes`) |
| `agent.disabled` | throttled kill-switch audit | `decision/decision.go:796` |
| `agent.intervention.effect` | child of the decision span context | `decision/effect.go:312-331` |
| `agent.llm.retro` | own root, or child of `agent.experiment`; Link to the game; event `retro.suggestion`. Capture-only spans carry `inference.used=none` and `inference.fallback_reason` = `no_backend_available` / `llm_not_eligible` / `agent_disabled` (kill switch off, #1270) | `decision/retro_capture.go` |
| `agent.game.summary` | own root or child of `agent.experiment` | `gamesummary/coordinator.go:195` |
| `agent.experiment` | long-lived **root**; its traceparent is injected into shadow-game spawns | `experiment/registry.go:657` |
| `experiment.fitness`, `experiment.evaluate`, `experiment.verdict` | children of `agent.experiment` | `experiment_loop.go:869-967` |
| `agent.infrastructure.decision` | per active infra cycle | `decision/infra_loop.go:478` |
| `agent.shadow_game.run` | legacy single shadow game | `gamerunner/runner.go:335` |
| `code_improvement.proposed`, `code_improvement.propose_attempt`, `code_improvement.synthetic_validation`, `code_improvement.apply` / `.discard`, `code_improvement.promote` | proposal, validation, promotion. `propose_attempt` carries `code_improvement.outcome` = `proposed` / `no_backend` / `undecodable` / `blocked` / `disabled` (+ `code_improvement.reason` when the kill switch flipped during the model call, #1270); `gated`, `llm_gated` and a `disabled` cycle before the call emit no span | `experiment/gate.go:117`, `experiment/proposer.go`, `experiment/validate.go:309-419`, `promote/promote.go:424` |
| `chat {model}` | CLIENT; decide backends (claude, openai, jev) and the retro / proposer backend | `decide/backends/*/`, `inference/claude.go:182` |

```text
[game trace]  <mode root> ─ gameplay_phase ─(remote parent)─> agent.signal_received
                                                                ├─ agent.decide (rules)
                                                                └─ agent.decision ─> agent.action
              gameplay_phase ─(remote parent)─> agent.decide (remote) ─┬─ chat {model}
                                                                       └─ agent.decision ─> agent.action
[experiment trace]  agent.experiment ─┬─ <shadow game root> ─ ... (agent spans of shadow games)
                                      ├─ experiment.fitness / experiment.evaluate ─ experiment.verdict, code_improvement.promote
                                      ├─ agent.llm.retro ─ chat {model}
                                      └─ agent.game.summary
```

`agent.span_received` no longer exists (#1053).

### 4.2 GenAI semantic conventions

| Span | `gen_ai.*` usage |
|---|---|
| `chat {model}` (decide) | `gen_ai.operation.name=chat`, `gen_ai.provider.name` (`anthropic`; openai-compatible: derived from the base URL host; `jev`), `gen_ai.request.model`, `gen_ai.request.max_tokens`, `gen_ai.output.type=json`, `gen_ai.request.temperature`, `gen_ai.request.reasoning.level`, `gen_ai.response.id` / `.model` / `.finish_reasons`, `gen_ai.usage.input_tokens` / `.output_tokens` / cache / reasoning tokens, `error.type` |
| `chat {model}` (jev) | `gen_ai.provider.name=jev`, request / response model, usage tokens, `gen_ai.response.id` from the `x-typesafe-request-id` header, plus `jev.native_confidence` (JSON map of Jev's native confidences, PR #1261) and `jev.stage` = `intervention` / `details` / `single` (one span per HTTP call of the two-stage round, PR #1265); a gated stage-1 span adds `jev.gate.p_yes` (the gate noul's P(yes)), `jev.stage1.choice` and `jev.stage1.distribution` (Jev's real pick and JSON distribution over the interventions, noop excluded; the `ffa.intervention` answer in `decide.answers` is synthesised from them, #1281) |
| `chat {model}` (retro / proposer) | same minus `gen_ai.output.type`; no content capture; no client metrics |
| `agent.decide` | `gen_ai.agent.name=joustmania-agent`, `gen_ai.conversation.id=<game id>`, `gen_ai.request.model`; event `gen_ai.evaluation.result` (`gen_ai.evaluation.name=act_confidence_threshold`, score value, label `pass` / `fail`, explanation naming the effective threshold and its flag). No `gen_ai.operation.name` (semconv suggests `invoke_agent`). |
| `agent.llm.retro` | `gen_ai.operation.name=chat`, `gen_ai.request.model` |

Content capture: `gen_ai.system_instructions`, `gen_ai.input.messages`, `gen_ai.output.messages` only when
`decision.capture_content` is on (default off; on in `ci/`), decide backends only, 16 KiB per part
(`services/agent/decide/llmio/genai.go`). Not gated: the retro span always carries the capped prompt in
`llm.retro.user`, and the system prompt is logged once per SHA (`agent.llm.system_prompt`) (D-T8).

Metrics `gen_ai.client.operation.duration` (s) and `gen_ai.client.token.usage` come from the decide backends.

### 4.3 Decision attributes

`decide.*` (backend, backend.requested, fallback_reason, state.digest, questions, answers, confidence,
confidence.min, act_confidence_threshold, act_confidence_threshold.source (#1287), latency_ms, outcome, rationale, invalid_reason, discarded_reason,
response_format, blocked, block_reason (#1295)) are specified in [decide-contract.md §12](decide-contract.md#12-telemetry).
`decision.*`: `decision.action`, `.reason`, `.objective_served`, `.blocked`, `.block_reason`,
`.intervention_id`. Targeted player (#1295), on `agent.decision` and `agent.action` only when
`Decision.TargetSerial` is set: `player.serial` (the coordinator's key, verbatim) and `player.name` (the display
name from `controller_info{serial,name}`, §4.4; absent when no name was seen for the serial, and never set for a `shadow` game, whose mock serials collide with real mock games' serials). `intervention.*`: `id`, `type`, `objective`, `signal`, `fitness_baseline`,
`fitness_followup`, `fitness_delta`, `window_seconds`, `effect_aborted`.

### 4.4 What the agent ingests

| Signal | Matched on | Source |
|---|---|---|
| Metrics | names `game_player_accel_magnitude`, `controller_battery_level`, `controller_battery_pct`, `game_player_playstyle`, `game_player_alive`, `controller_connected`, `game_duration_seconds`, `game_active_players`, `game_active`, `game_current_mode`, `game_player_deaths_total`, `game_player_peak_accel`, `game_trace_correlation`, `game_music_tempo`, `game_effective_death_threshold`, `game_effective_warning_threshold`, `game_player_movement_variance`, `game_player_movement_variance_aggregate`, `game_player_skill_level`, `game_player_elimination_order`; `controller_info` (labels `serial`, `name`: identity only, kept process-wide as the serial → display-name view for `player.name`, never routed to a game partition and never a trigger, #1295); labels `serial`, `mode`, `game_id`, `game_kind`, `game_trace_id`, `game_trace_span_id`, `gameplay_phase_span_id`, `experiment_id`, `arm` | `services/agent/gamecontext/extract_metrics.go:13-81`; `controller_info`: `gamecontext/multiplexer.go` (`PlayerName`) |
| Spans | any span: `player.serial`, `game.mode`, `game.id`, `game.kind`, `experiment.id`, `experiment.arm`; event `player_death` | `gamecontext/extract_spans.go:10-135` |
| Infra spans | `controller.bluetooth_health` + event `bluetooth_controller_sample` | `infracontext/extract_spans.go:14-78` |

Renaming any of these breaks the agent silently: update this table and the extractor in the same PR.

**Elapsed game time.** The agent's in-game elapsed time (`elapsed_s` in the decide state, `duration` in the rules
and the `agent.evaluate` log line) is `game_duration_seconds` while that gauge is fresh, i.e. its value advanced
within the last 5 s. When the gauge is missing or stale during an active game, the agent derives the elapsed time
from when it saw `game_active` go 0 → 1, never going below the last gauge value. Within a game the value never
decreases. At game end the duration every game-end consumer (experiment conclusion, retro, summary, proposer) sees is
the larger of the gauge and the agent's own start → end time, because the coordinator's final set can arrive after
`game_active` 0 (UC-041 A3, BR-003; `services/agent/gamecontext/store.go` `effectiveDurationLocked`).

**Traceability:** FR-040 to FR-049, FR-056, FR-063, FR-071, FR-072 / UC-040 to UC-045, UC-050, UC-051 / `services/agent/decision/`, `services/agent/experiment/`, `services/agent/gamecontext/` / `services/agent/decision/*_test.go` (trace_link, signal_telemetry, gameplay_phase_parent), `tests/integration/test_decide_backend_e2e.py`, `tests/integration/test_experiment_cohort_loop.py`.

## 5. Game events

Published on each game's event bus (`StreamGameEvents`) and recorded as span events on the current span
(`services/game_coordinator/event_bus.py:135`):

| Group | Event types | Source |
|---|---|---|
| Coordinator | `game_start`, `game_start_error`, `game_error`, `game_force_ended`, `agent_intervention` | `servicer.py:631,755,780`; `game_session.py:406-540`; `interventions.py:1254` |
| Lifecycle | `game_starting`, `players_initialized`, `countdown_start`, `countdown_end`, `game_started`, `player_revived`, `game_ended`, `game_winner`, `game_tie`, `player_analytics`, `player_shielded` (`serial`, `kind` = full / partial, `seconds`, `until_ms`; UC-040 A16) | `games/base.py`, mode files |
| Teams | `team_winner`, `team_formation_start`, `team_formation_end` | `teams_base.py:348`, `random_teams.py:198,222` |
| Traitor / Werewolf | `traitor_signal_start` / `_end`, `traitor_winner`, `werewolf_intro_start` / `_end`, `werewolf_reveal`, `werewolf_winner` | `traitor.py`, `werewolf.py` |
| Zombies / Swapper | `zombie_intro_start` / `_end`, `time_announcement`, `human_converted`, `zombie_winner`, `player_swapped`, `swapper_winner` | `zombie.py`, `swapper.py` |
| 1v1 | `round_start`, `round_end`, `round_timeout`, `tournament_intro`, `match_start`, `match_end`, `tournament_champion`, `tournament_no_winner` | `fight_club.py`, `tournament.py` |
| NonStop, JoustFFA | `player_death` (`serial`; FFA adds `alive_remaining`, used by the agent for the pattern-game elimination order, UC-046 BR-014), `player_respawned` (NonStop) | `nonstop_joust.py:285,519` |

Defined in `lib/types.py` but never published: `player_revive`, `player_out`, `score_update`,
`team_eliminated`. Game-ending events for the menu: `game_ended`, `game_force_ended`, `game_error`.

**Traceability:** FR-020 to FR-028 / UC-020 to UC-027 / `services/game_coordinator/` / `tests/integration/test_full_game_lifecycle.py`, `tests/integration/test_parallel_lifecycle.py`.

## 6. Metrics

Python metrics are created through `lib/otel_metrics.py` (`Counter`, observable `Gauge`, `Histogram`; no unit
argument). 150 unique Python names; the main ones per service:

| Service | Metrics (type) | Source |
|---|---|---|
| all Python | `process_cpu_seconds_total` (C), `process_resident_memory_bytes` (G), `process_threads` (G), `grpc_requests_total` (C), `grpc_request_duration_seconds` (H), `grpc_client_request_duration_seconds` (H), `grpc_client_errors_total` (C), `flag_eval_errors_total{flag_key,reason}` (C) | `services/*/metrics.py`, `lib/grpc_metrics.py`, `lib/flag_eval_visibility.py:47` |
| game-coordinator | `game_active`, `game_duration_seconds`, `game_active_players` (G, labels `game_kind, game_id, experiment_id, arm`); `game_trace_correlation` (G); `games_started_total`, `games_completed_total` (C, `mode, game_kind`); `game_player_*` (accel, peak, playstyle, alive, movement variance / zone, skill, elimination order) (G, `serial, game_id`); `game_player_deaths_total` (C); `game_effective_warning_threshold`, `game_effective_death_threshold`, `game_music_tempo`, `game_sensitivity` (G); `game_actual_update_frequency_hz`, `game_current_update_frequency_hz`, `game_loop_jitter_ms`, `game_loop_frame_consistency_percent` (G); `game_loop_frames_dropped_total` (C); `game_interventions_total{type,objective,blocked,block_reason}` (C); `game_flag_evaluations_total`, `game_flag_configuration_changes_total` (C); `game_shadow_games_blocked_total`, `game_shadow_games_preempted_total` (C) | `services/game_coordinator/metrics.py` |
| controller-manager | `controller_connected`, `controller_battery_level`, `controller_battery_pct`, `controller_info{serial,name}` (one series per tracked controller: replaced on `RenameController`, removed when the controller is no longer tracked, #1295), `controller_backend_info`, `controller_adapter_info`, `controller_polling_target_hz`, `controller_state_update_hz`, `controller_accel_magnitude` (G); `controller_disconnect_total`, `controller_reconnect_total`, `controller_routing_decisions_total{serial,adapter,method}`, `controller_poll_drops_total`, `controller_poll_errors_total`, `controller_chaos_faults_injected_total`, `controller_button_events_total` (C); `controller_input_lag_seconds`, `controller_discovery_check_duration_seconds`, `controller_poll_batch_duration_seconds`, `controller_bluetooth_event_gap_seconds` (H) | `services/controller_manager/metrics.py` |
| menu | `menu_player_ready`, `menu_idle_mode_active`, `menu_stream_connections_active` (G); `menu_button_presses_total`, `menu_ghost_controllers_pruned_total`, `menu_force_start_reconcile_failed_total`, `menu_button_event_errors_total{event_type}` (C) | `services/menu/metrics.py` |
| audio | `audio_sounds_played_total{result}`, `audio_playback_errors_total`, `audio_channel_saturation_total` (C); `audio_channels_active`, `audio_music_playing`, `audio_music_tempo` (G) | `services/audio/metrics.py` |
| pairing-daemon | `psmove_pairing_attempts_total` / `_success_total` / `_failed_total` (C); `bluetooth_device_rssi_dbm{serial,hci_adapter}`, `bluetooth_device_connected`, `bluetooth_adapter_connections` (G); `psmove_pairing_duration_seconds` (H) | `services/pairing_daemon/psmove_pairing/metrics.py` |
| agent | `agent_llm_gated_total{reason}` (decide path, retro and, since #1270, the experiment proposer; decide-path reasons `llm_not_eligible`, `llm_budget_exhausted`, `decide_interval` and, since #1298, `no_interventions_allowed` (empty allow-list, no remote call, decide-contract §7.1)), `agent_evaluations_total{signal,outcome}`, `agent_decisions_total{action,blocked,block_reason}`, `agent_interventions_applied_total{action}`, `agent_intervention_writes_total{result}`, `agent_intervention_file_corruption_total{phase}` (C); `agent_intervention_effect_delta` (H); `gen_ai.client.operation.duration`, `gen_ai.client.token.usage` (H) | `services/agent/decision/metrics.go`, `actions/writer.go:199-227`, `decide/backends/claude/metrics.go` |

`game_duration_seconds` is the seconds since the game was started (StartGame, so it includes the countdown). The
coordinator refreshes it every 1 s while the session is RUNNING (real and shadow sessions alike), sets the final
value when the game loop ends, and removes the series when the session retires. It carries the same per-game label
tuple as `game_active` (`game_kind, game_id, experiment_id, arm`), so there is one series per live game (UC-041
BR-003; `services/game_coordinator/game_session.py` `_publish_duration_while_running`).

### 6.1 Consumers

| Consumer | Uses | Source |
|---|---|---|
| Grafana (20 dashboards, datasource VictoriaMetrics) | agent-operations, interventions, game-quality, game-analytics, player-insights, controller-overview, backend-comparison, bluetooth-adapter, feature-flags, fleet-overview, host-metrics, presentation-chaos, ... Every referenced app metric exists; no dashboard queries Loki or Jaeger. | `services/grafana/dashboards/` |
| Prometheus alerts | 16 alerts; `GameCrashRate` and `ServiceUpdatePending` use undefined metrics (D-T3) | `services/prometheus/alerts.yml` |
| Dashboard SPA | Jaeger `service=agent operation=agent.decide` (tags `decide.*` incl. the `decide.answers` shape, `gen_ai.request.model`, `gen_ai.response.model`, `game.id`, `game.kind`, `decision.*`, `player.serial` / `player.name` on `agent.decision` for the Decisions row target (#1295); a child is a chat span when its name starts with `chat ` or `gen_ai.operation.name=chat`); Loki agent log lines (§7); VictoriaMetrics `sum(agent_intervention_writes_total) by (result)` | `services/dashboard/src/agent.ts`, `experiments.ts` |
| Agent | §4.4 | |
| Integration tests | span names `agent.decide`, `agent.signal_received`, `agent.decision`, `chat {model}`, `agent.disabled`; `decide.*` tags; event `gen_ai.evaluation.result` | `tests/integration/test_decide_backend_e2e.py` |
| Demo validation harness | span chain `agent.signal_received` → `agent.decision` → `agent.action` per game; Prometheus queries | `tools/demo/validation_harness.py` |
| Talk pre-flight | Jaeger `service=agent operation=agent.decide`, tags `decide.backend`, `decide.outcome`, `decide.fallback_reason`; flag **variant names** of the stage baseline (`ambient`, `default`, `haiku`, `real_only`, ...) | `scripts/talk-preflight.sh` |

80 defined metrics have no consumer (all `menu_*`, `psmove_*`, `python_hid_*`, `audio_*` and others); they are
kept for ad-hoc analysis.

**Traceability:** FR-081, FR-083, FR-084, NFR-022 / UC-035 / metrics files above / `services/*/tests/` (metric patches), `tests/integration/test_decide_backend_e2e.py`.

## 7. Log lines that are part of the contract

Agent logs are slog logfmt; consumers match the message **and parse the listed keys**. Renaming a message or a
key breaks them.

| Message | Keys consumers parse | Consumers | Emitted at |
|---|---|---|---|
| `agent.evaluate` | `session_id`, `game_mode`, `decision_backend`, `battery_threshold`, `variance_window`, `max_per_minute` (also emitted: `player_count`, `duration`) | dashboard Agent tab (Loki `{service_name="agent"}`, `services/dashboard/src/agent.ts`); `tools/demo/validation_harness.py:397` (`decision_backend=`); `tests/integration/test_experiment_cohort_loop.py:133-136` (`decision_backend=rules`) | `services/agent/decision/decision.go:494-503` |
| `agent.decision_blocked` | `intervention`, `reason` (also: `blocked`, `target_serial`, `decision_reason`, `allowed`) | dashboard Agent tab; `tools/demo/validation_harness.py:398` | `services/agent/decision/decision.go:723-729` |
| `agent.rollout_expand`, `agent.rollout_rollback`, `agent.rollout_rollback_recommended` | message only | dashboard Agent tab | `services/agent/decision/infra_loop.go:498-506` |
| `experiment.game_assigned` | `experiment_id`, `game_id`, `arm`, `pair_index` | dashboard Experiments tab (`services/dashboard/src/experiments.ts`) | `services/agent/experiment/registry.go:1030` |
| `experiment.game_concluded` | `experiment_id`, `game_id`, `arm`, `fitness` | Experiments tab; `tests/integration/test_experiment_cohort_loop.py:453` (`fitness=`, `arm=control`, `arm=experimental`) | `registry.go:1146` |
| `experiment.concluded` | `experiment_id`, `outcome` | Experiments tab; cohort-loop test | `registry.go:1336` |
| `experiment.torn_down` | `experiment_id`, `status`, `reason` | Experiments tab; cohort-loop test | `registry.go:1481` |
| `experiment.declared`, `code_improvement.promoted`, `experiment.promote_failed`, absence of `panic:` | message only | `tests/integration/test_experiment_cohort_loop.py` | `registry.go:560,1387`, `promote/promote.go:475` |
| `Proposer/retro inference backend selected` | `backend` (`backend=stub` asserted) | `tests/integration/test_experiment_cohort_loop.py:131-132` | `services/agent/main.go:559` |
| `agent.llm.system_prompt`, `agent.llm.retro_response`, `agent.llm.retro_captured`, `agent.llm.retro_failed` | prompt SHA, response | prompt resolution by hand | `decision/prompt_fingerprint.go`, `decision/retro_capture.go` |
| `Agent intervention writes gated by LIVE interventions_enabled flag` | message only | runbooks | `services/agent/main.go:149` |

Agent logs reach Loki through `filelog/docker` with `service_name` from the compose label; they carry no
trace id (D-T9).

**Traceability:** FR-082, FR-084 / UC-035, UC-043, UC-070 / files above / `tests/integration/test_experiment_cohort_loop.py`.

## 8. Naming rules

Rules (from `.claude/rules/development.md`): metric names prefixed with the service (`game_`, `controller_`,
`menu_`, `audio_`, `agent_`), counters end in `_total`, histograms in `_seconds` / `_bytes` (or a base unit),
span names in dot notation for new spans, attributes namespaced as in §2.1.

Known violations on `main` (do not copy them; fix opportunistically, D-T6): gauge `active_controllers_total`;
`games_*`, `bluetooth_*`, `event_bus_*` without a service prefix; `audio_sounds_played_total` and
`audio_playback_errors_total` emitted by both audio and game-coordinator with different labels; histograms
`controller_streamed_per_frame`, `controller_poll_batch_size`, `controller_led_controllers_updated_per_batch`,
`game_accel_distribution`, `agent_intervention_effect_delta`; `game_loop_jitter_ms` in milliseconds; most
coordinator / menu spans in snake_case; rust `grpc.server` vs Python `{Service}/{Method}`.

**Traceability:** NFR-030, NFR-031 / UC-035 / `.claude/rules/development.md`.

## 9. Pattern games (#1258)

Shipped by PR #1308 for [UC-046](../use_cases/UC-046.md). New
namespace `pattern.*` (producers: controller-manager, agent). Goal: a pattern game can be replayed from its trace
alone, and the trace proves whether the inputs matched.

| Producer | Span / event | Attributes | When |
|---|---|---|---|
| controller-manager | span `controller.mock.pattern_install` | `pattern.archetype`, `pattern.stream_rate_hz`, `pattern.players`, `pattern.params` (the parameter object as canonical JSON: sorted keys, no whitespace), `mock.tag`; status error plus `pattern.validation_errors` when the parameters are invalid | `AddControllers` with `archetype` |
| controller-manager | event `pattern.slot` on `controller.mock.pattern_install`, one per controller | `pattern.slot_index`, `controller.serial` | same |
| controller-manager | span `controller.mock.pattern_release` | `controller.serial`, `pattern.archetype`, `pattern.slot_index`, `pattern.frames_delivered`, `pattern.signal_hash`, `pattern.signal_checkpoints`, `pattern.frame_overruns` | `RemoveController` of a pattern controller |
| agent | event `experiment.pattern_pinned` on `agent.experiment` | `pattern.selection` (flag value as read), `pattern.archetypes` (pinned names in pair order), `pattern.params_digests` (same order), `pattern.max_game_seconds` | first spawn of an experiment |
| agent | event `experiment.pattern_game` on `agent.experiment`; the same attributes on `agent.shadow_game.run` for a one-shot game | `game.id`, `experiment.arm`, `experiment.pair_index`, `pattern.archetype`, `pattern.params_digest`, `game.rng_seed` (decimal string), `pattern.stream_rate_hz`, `pattern.slot_serials` (serials in slot order), `pattern.max_game_seconds`, `pattern.flag_digest` (SHA-256 hex of the canonical resolved values of the flags listed in UC-046 BR-011, or `unavailable`) | each spawned arm |
| agent | event `experiment.pattern_game_ended` on `agent.experiment` (or `agent.shadow_game.run`) | `game.id`, `pattern.archetype`, `pattern.params_digest`, `pattern.outcome` (`winner`, `tie`, `timeout`, `aborted`, `error`), `pattern.censored` (true for `timeout`), `pattern.elimination_order` (slot indices in the order of the coordinator's `player_death` events), `pattern.winner_slot` (−1 = none), `pattern.order_source` (`events` or the `frames` fallback), `pattern.signal_hashes` (slot order), `pattern.replay_exact`, `pattern.non_exact_reasons` (`remote_backend`, `frame_overrun`, `concurrent_game`, `kill_switch`, `unseeded_draws`), `pattern.decisions` (`intervention:slot` in order), `pattern.flag_digest` | game end |
| agent | `agent.decision` in a pattern game | `pattern.target_slot` (slot of the targeted player, when the target is a player), `decision.chaos_draw_index` (number of chaos draws of the game's rules engine so far) | each decision |

**Signal hash:** FNV-1a/64 over the controller's delivered frames in order, each frame encoded as the signed
32-bit little-endian integer ⌊intensity · 1000 + 0.5⌋ (milli-g, rounded half up); rendered as 16 lowercase hex digits.
intensity is computed in IEEE-754 double precision; the milli-g rounding keeps the hash stable across Python builds,
and golden tests pin it. **Checkpoints:** the hash value after every r delivered frames (one per second of game time).
The total number of delivered frames depends on wall-clock lag (a dead player leaves the stream filter a few frames
after its death; the winner's stream runs until the game is torn down), so replays compare the checkpoints on their
common prefix, not the final hash.

**Journal:** the experiment journal records the same facts: a `pattern_pinned` event (selection, pinned names,
parameter objects, parameter digests, safety cap and a note when the cap fell back to 300), `archetype`,
`params_digest`, `rng_seed` and `flag_digest` on each `game_assigned`, a `pattern_game_ended` event (outcome,
`censored`, elimination order and its source, winner slot, signal hashes, checkpoints, replay classification, decision
sequence) per pattern game, and a `pattern_pair_rerun` / `pattern_archetype_skipped` decision note when a pair runs
again or an archetype is skipped (UC-046 BR-015). The experiment's conclusion (`outcome` event) reports the timeout
count and rate per arm and whether one arm timed out clearly more often (UC-046 BR-013).

**Liveness check (BR-008):** the runner asks the agent's telemetry multiplexer at the game's start, once per
second while it runs and at its end whether another partition reports `game_active` with an update in the last
10 s; stale partitions are ignored.

**Replay check:** read `pattern.archetype`, `pattern.params_digest`, `game.rng_seed`, `pattern.stream_rate_hz`, the
game mode and sensitivity off the trace, run one shadow game with them (`SHADOW_GAME_ARCHETYPE`, `SHADOW_GAME_SEED`),
and compare: per-slot checkpoints equal on their common prefix (NFR-040), `pattern.elimination_order` and
`pattern.winner_slot` equal (NFR-041), and the sequence of rules decisions as (`intervention` type,
`pattern.target_slot`) equal (NFR-042). Only games with `pattern.replay_exact=true` and the same
`pattern.params_digest` and `pattern.flag_digest` on both sides are compared. `scripts/pattern-replay-check.sh
<archetype> [seed]` runs the archetype twice with the one-shot runner and performs this comparison on the two runs
(it reads the `PATTERN_REPLAY_RECORD` line the one-shot runner prints), and `scripts/pattern-replay-check.sh --against
<record>` replays one recorded game: the record is a saved `PATTERN_REPLAY_RECORD` JSON or a `pattern_game_ended`
journal event (a line of the experiment's `events.jsonl`). The checkpoint hashes prove identical input; the
elimination order, winner and decisions are the evidence that the game played out the same (UC-046 BR-001).

**Traceability:** FR-107, FR-108, FR-109, FR-110, NFR-040 to NFR-042 / UC-046 / `services/controller_manager/mock_control_service.py`, `services/agent/experiment/registry_pattern.go`, `services/agent/gamerunner/operations.go`, `services/agent/decision/pattern_hook.go`, `services/agent/pattern_wiring.go` / `services/agent/gamerunner/pattern_test.go`, `services/agent/decision/pattern_hook_test.go`, `services/agent/experiment/registry_pattern_test.go`.
