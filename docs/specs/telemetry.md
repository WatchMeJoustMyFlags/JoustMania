# Telemetry

> Technical spec. Part of the [spec tree](../README.md). State: `main` @ 2b8d979c (2026-10-16, includes PRs #1256, #1257, #1259, #1261, #1265, #1297, #1303, #1307, #1309, #1312, #1315, #1324, #1308 (#1258 pattern games), #1332 (#1325 span parents), #1378 (validator spans removed), #1351 (#1326 budget), #1333 (#1327 one `player_death` event), #1375 (rollout dry run), #1376 (deaths counter, input lag), #1377 (quiet-cycle fallbacks) , #1387 (`audio.silent`) and #1370 (`game.outcome`)). The metric catalog (§6.2) and the span-event inventory were generated from the code at 84c3ba15 by script; remaining `file:line` references were re-based to 84c3ba15 and then to 2b8d979c.
> Telemetry is the agent's only view of the game (C-001), so span names, attribute keys, metric names and the
> log lines below are a **contract**: the agent, the dashboard SPA, Grafana and integration tests match
> on them. Change a name here first.

## 1. Pipeline

| Stage | What | Source |
|---|---|---|
| Producers | Python services: OTLP traces (`OTLPSpanExporter`), OTLP metrics (`OTLPMetricExporter`, interval from `metrics_export_interval_ms`), OTLP logs with trace ids; Go agent / connect-proxy and rust-hid: OTLP, metrics every 1 s (their only app-independent metrics are `process_cpu_seconds_total`, `process_resident_memory_bytes`, `process_threads`; the agent adds the §6 `agent_*` and `gen_ai.*` metrics) | `lib/telemetry.py`, `lib/otel_metrics.py:85-154`, `lib/otel_logging.py`, `services/agent/otel.go:initMetrics`, `services/rust-hid/src/main.rs` (process metrics) |
| Collector receivers | `otlp` (4317 / 4318), `prometheus/observability`, `prometheus/infrastructure` (flagd 8014, envoy 9901), `filelog/docker` (all container logs), `hostmetrics`, `docker_stats` | `services/otel-collector/config.yaml:2-153` |
| Processors | `memory_limiter` (600 MiB, spike 150 MiB, env-overridable), batch (traces 2 s, metrics 100 ms, logs 5 s), resource attributes; **no sampling** | `config.yaml:160-309` |
| Exporters | Jaeger (OTLP), VictoriaMetrics (`prometheusremotewrite`), Prometheus exporter :8889, Loki (OTLP HTTP), **agent** (`otlp/agent`, 2 s timeout, no retries, queue 1000) | `config.yaml:317-355` |
| Pipelines | traces → Jaeger + agent; OTLP metrics → VictoriaMetrics + Prometheus exporter + agent; other metrics → VictoriaMetrics; logs → Loki (agent gets no logs) | `config.yaml:358-406` |
| Prometheus | scrapes the collector every 500 ms, controller-manager `:8000`, flagd; global 10 s; no alert rules (Prometheus alerting removed in #1425, FR-083 Rejected) | `services/prometheus/prometheus.yml` |
| Retention | VictoriaMetrics 7 d, Prometheus 30 d, Loki 24 h, Jaeger bounded badger TTL | `docker-compose.yml:519,551`; `services/loki/config.yaml` (`retention_period`); `services/jaeger/config.yaml` |

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
| `controller.*`, `bluetooth.*`, `identify.*` | `controller.serial`, `controller.adapter`, `identify.outcome`, `bluetooth.event_gap_ms`, `bluetooth.dropped_events_pct`, `bluetooth.movement_update_hz`, `bluetooth.target_backend`, `bluetooth.rollout_count`, `bluetooth.rollout_strategy`, `bluetooth.report_gap_p95_ms`, `bluetooth.report_gap_max_ms`, `bluetooth.connection_quality` | controller-manager |
| `experiment.*`, `verdict.*` | `experiment.id`, `experiment.arm`, `experiment.objective`, `experiment.flag_key`, `experiment.target_n`, `experiment.thesis`; `verdict.outcome`, `verdict.delta`, `verdict.significant` | game-coordinator, agent |
| `agent.*`, `decision.*`, `decide.*`, `intervention.*`, `policy.*`, `inference.*`, `llm.*` | see §4 | agent |
| `gen_ai.*` | OTel GenAI semantic conventions, see §4.2 | agent |
| `rpc.*`, `server.*` | OTel RPC semantic conventions on gRPC spans | all |

### 2.2 Correlation ids

| Id | On spans | On metrics | Notes |
|---|---|---|---|
| game id | `game.id` (agent also `session.id`) | label `game_id` on per-game and per-player metrics; the game-wide gauges of §6 (tempo, thresholds, sensitivity, mode, loop rate) have none (#1336) | `game_<12 hex>`; the agent partitions all signals by it |
| game kind | coordinator: `game.kind` = `primary` / `shadow`; agent: `game.kind` = `real` / `shadow` | coordinator label `game_kind` = `primary` / `shadow` | **Two vocabularies under one key** (#1249). Flags use `real` / `shadow`; the agent normalises `primary → real` at ingest (`services/agent/gamecontext/gamekind.go`). |
| experiment, arm | `experiment.id`, `experiment.arm` | `experiment_id`, `arm` on per-game gauges only (never on cumulative counters) | |
| trace context | `game.trace_id`, `game.trace_span_id`, Links | gauge `game_trace_correlation{game_trace_id, game_trace_span_id, gameplay_phase_span_id, game_cycle_span_id}` = 1 while live; the two phase ids are empty until their span opens and the gauge is re-emitted when each is published (UC-041 BR-004) | metric-carried because observable gauges cannot carry exemplars (`services/game_coordinator/metrics.py:47-100`) |
| serial | `player.serial`, `controller.serial` | `serial` | `player.serial` is ONE key on both sides (#1295): the coordinator's player spans and intervention events, and the agent's targeted `agent.decision` / `agent.action`, so one Jaeger search `player.serial=<serial>` finds both |
| intervention | `intervention.id`, `decision.intervention_id` | | joins decision and effect |
| W3C propagation | menu events carry `_traceparent` / `_tracestate`; the agent injects traceparent into shadow-game spawns and model calls | | |

**Traceability:** FR-041, FR-043 / UC-035, UC-041 / `lib/feature_flags.py:63-64`, `services/game_coordinator/game_session.py:56-57,386-395`, `services/agent/gamecontext/` / `services/agent/gamecontext/*_test.go`.

## 3. Spans

### 3.1 gRPC (all Python services, rust-hid)

| Span | Kind | Gate | Source |
|---|---|---|---|
| `{Service}/{Method}` (e.g. `GameCoordinatorService/ForceEndGame`) | CLIENT / SERVER | flag `grpc_rpc_spans` (default variant `enabled`), evaluated per RPC; when flagd is unreachable or the flag is missing the Python check falls back to **false** (no RPC spans) | `lib/grpc_tracing.py` (`_rpc_spans_enabled`) |
| `grpc.server` (rust-hid) | SERVER | same flag | `services/rust-hid/src/grpc_tracing.rs:105-115` |
| renamed: `StreamButtonEvents`, `StreamGameplayData` (CM); `PlaySound:{sound}`, `PlayMusic:{dir}`, `ChangeTempo:{x}x`, `SetVolume:{pct}` (audio) | SERVER | | `services/controller_manager/servicer.py` (`StreamButtonEvents`, `StreamGameplayData`); `services/audio/servicer.py` (`PlaySound`, `PlayMusic`, `ChangeTempo`, `SetVolume`) |

Audio span attributes: `PlaySound:{sound}` carries `audio.file`; `PlaySound` and `PlayMusic` carry `audio.muted=true` when `play_audio` is off (read at use time); `PlaySound` carries `audio.silent=true` when `play_audio` is on but the audio hardware is not open yet, so nothing is heard (#1387).

connect-proxy: `otelhttp` server spans (`/health` filtered) and `otelgrpc` client spans only.

### 3.2 game-coordinator

Flag-provider readiness at game admission (#1329, UC-010 A5 / BR-004): the game root span carries `feature_flag.providers_wait_ms`, the time spent waiting for the coordinator's NOT_READY flag providers (`system`, `controller`, `game`, `agent`, `user`, `interventions`; STALE / ERROR are not waited on) to be READY before the game was created. When that bounded wait (5 s) runs out, the root span gets the event `feature_flag.providers_not_ready` (attrs `feature_flag.domains`, the domains still not READY; `feature_flag.wait_seconds`; `feature_flag.fallback` = `code_defaults`). Flag reads of those domains then fall back to code defaults and show as `ProviderNotReadyError` exceptions on the span where they were read.

| Span | Notes | Source |
|---|---|---|
| `<mode display name>` (e.g. `Joust Free-for-All`, `Non Stop Joust`) | **game root**, own trace (#1157); child of `agent.experiment` for experiment shadow games (#1182); attrs `game.name`, `game.id`, `game.kind`, `experiment.id`, `experiment.arm`, `player.count`; at game end `game.outcome` (`winner`, `tie`, `force_ended`; `completed` for modes that do not classify their natural end yet), `game.end_reason` (the force-end reason, only for `force_ended`), `game.winner` (serial, only for `winner`); latched when the game loop ends, so a later force-end does not rewrite it; recorded by every mode, including the `run()` overrides (Fight Club, Werewolf, Zombies, Tournament) (#1328, UC-020 BR-003) | `game_session.py:_run_game_loop_async` |
| `initialization_phase`, `<phase>` (`color_assignment`, `team_formation_phase`, `traitor_signal`, `werewolf_intro`, `zombie_intro`, `fight_club_intro`, `tournament_intro`), `gameplay_phase`, `players`, `game_cycle`, `teardown_phase` | phases; `game_cycle` (child of `gameplay_phase`, base modes) is the remote parent of in-game agent spans (#1325); modes without it fall back to `gameplay_phase`, then the game root (#1195); `players` carries `player_count` (not `player.count`). The game root records span event `game.state_changed` (attrs `game.state`, `game.id`, plus `player.count` when RUNNING and `game.error` on an error) at each state change; `gameplay_phase` records event `music_tempo_change` (`old_tempo`, `new_tempo`, `direction`) next to each tempo span; `teardown_phase` carries event `game.outcome` with the root span's outcome attributes (#1328) | `games/base.py:run` |
| `player_lifecycle`, renamed `player: {name}` on the first frame | one per player; attrs `player.serial`, `player.name` (set with the rename), `game.mode` (`GAME_MODE_ATTR`, set by the modes that build their own player spans: werewolf, zombie, fight club, tournament, swapper, team modes); events `death_warning` (attrs `accel_magnitude`, `threshold`, `sensitivity`, `sensitivity_factor`, `music_speed`), `player_death`, `shield_granted`, `player_revived`, `partial_shield_armed`, `shield_feedback` (attrs `shield.kind` = full / partial, `shield.duration_ms`, `shield.sound` = `shield_up` or `none` on a silent extension; UC-040 A16), `soft_penalty_tighten`, `auto_rubberband_boost`, `sensitivity_factor_applied`, `handicap_factor_applied` (#1295; recorded only when the player's factor changes), `player_disconnected` (`reason`), `game_ended` (`survived`, `game_duration`; closes the span at game end). The events that record an applied intervention (`shield_granted`, `partial_shield_armed`, `soft_penalty_tighten`, `auto_rubberband_boost`, `player_revived`, `sensitivity_factor_applied`, `handicap_factor_applied`, and the base kill path's `player_death`, which carries `death.reason=agent_intervention` for `eliminate_player`) carry `player.serial` and, once the controller reported one, `player.name` (#1295). The span ends at game end, so these events reach Jaeger then (UC-041 BR-001). JoustFFA end events (#1328, UC-020 BR-003): `victory` (attrs `winner` = true, `game_duration`, `analytics.*`) only on the winner of a `winner` outcome; `player_survived` (attrs `winner` = false, `game.outcome` = `force_ended`, `game.end_reason`, `game_duration`, `analytics.*`) on each survivor of a force-ended game. | `games/base.py` (`player_identity_attributes`), `difficulty_handlers.py` |
| `countdown_phase` | per player (base modes) | `games/base.py:run` |
| `team_lifecycle` | team modes; closed with event `team_victory` or `team_survived` (`game_duration`, `winner`) | `games/teams_base.py:_create_player_spans`, `_end_surviving_team_spans` |
| `zombie_game`, `werewolf_game`, `fight_club_game`, `tournament_game`, `tournament_match` | modes with their own `run()` | mode files |
| `music_tempo_change`, `music_tempo_ramp` | attrs `old_tempo`, `new_tempo`, `dead_count` (`music_tempo_change` also `direction`) | `games/base.py:_apply_tempo_change`, `_apply_ramp_tempo` |
| `controller_poll_degraded`, `controller_led_degraded` | per player; `controller_poll_degraded` records event `poll_issues` per frame with drops | `games/base.py:_process_health` |

Mode-specific span events on the player (`player_lifecycle` / `player: {name}`) spans. Each mode's
`_kill_player_impl` records its own death event in addition to (or instead of) the base `player_death`:

| Mode | Span events (attributes) | Source |
|---|---|---|
| JoustFFA | `player_death` (`accel_magnitude`, `sensitivity`, `alive_count`), `victory` on the winner | `games/ffa.py:_kill_player_impl`, `_close_all_player_spans` |
| Team modes (JoustTeams, JoustRandomTeams) | `player_death` (+ `alive_count`, `team_eliminated`), `team_eliminated` (`last_player`, `alive_teams_count`), `player_survived` | `games/teams_base.py:_kill_player_impl`, `_end_surviving_player_spans` |
| Traitor | `player_death` (+ `is_traitor`, `visible_team`, `secret_team`), `team_eliminated` (`last_player`, `was_traitor`), `game_ended` (`winner`, `visible_team`, `secret_team`, `is_traitor`) | `games/traitor.py` |
| Werewolf | `player_death` (`is_werewolf`, `was_revealed`, `alive_count`), `werewolf_revealed` (`time_since_start`), `game_ended` (`winner`, `is_werewolf`, `winning_team`) | `games/werewolf.py:_kill_player_impl`, `_reveal_werewolves`, `_end_game_impl` |
| Zombies | `zombie_killed` (`accel_magnitude`, `respawn_delay`), `human_converted` (`accel_magnitude`, `remaining_humans`), `zombie_respawned`, `game_ended` (`winner`, `is_zombie`, `winning_team`) | `games/zombie.py` |
| Swapper | `team_swap_out` (`old_team`, `new_team`, `old_team_name`, `new_team_name`, `swap_count`), `team_swap_in` (`from_team`, `from_team_name`, `swap_count`), `game_ended` (`winner`, `team`, `excluded`) | `games/swapper.py` |
| NonStop | `player_death` (`kills`, `deaths`, `score`), `player_respawned` (`kills`, `deaths`, `score`), `final_score` | `games/nonstop_joust.py:_kill_player_impl`, `_respawn_player`, `_end_game_impl` |
| FightClub | `round_lost` (`accel_magnitude`, `round`, `winner`), `game_ended` (`winner`, `score`, `final_rank`) | `games/fight_club.py` |
| Tournament | `match_death` (`match_id`, `accel_magnitude`, `sensitivity`), `eliminated` (`match_id`, `round`, `eliminated_by`) and `match_complete` (`winner`, `loser`) on the match span, `tournament_ended` (`is_champion`, `wins`, `final_round`) | `games/tournament.py:_kill_player_impl`, `_run_match`, `_end_game_impl` |

Game events of §5 are additionally recorded as span events on whatever span is current when they are
published (`event_bus.py:publish`).

**`player_death` span event: exactly one per death (#1327).** Every death that reaches the base kill path (`BaseGameMode._kill_player`) records exactly one `player_death` event on that player's lifecycle span, in every mode. Before #1327 FFA, Teams / Random Teams, Traitor, Werewolf and NonStop also emitted a second, poorer `player_death` from their `_kill_player_impl`, so each death showed twice in Jaeger (dry run #2). The one event carries:

- always: `player.serial`, `player.name` (once the controller reported one), `death.reason` (`motion` for a threshold death, `agent_intervention` for `eliminate_player`), `accel_magnitude` (0.0 for a non-motion death), `alive_remaining` (alive players after this death), `sensitivity`, `sensitivity_factor`, `music_speed`;
- mode extras, added through `_player_death_event_attributes()` and computed as if the player were already out: Teams / Random Teams `team`, `team_eliminated`; Traitor `team_eliminated` (secret team), `is_traitor`, `visible_team`, `secret_team`; Werewolf `is_werewolf`, `was_revealed`; NonStop `kills`, `deaths` (including this death), `score`.

A mode's `_kill_player_impl` never adds its own `player_death` span event. Mode-specific outcome events stay separate (`zombie_killed`, `team_swap_out`, `match_death`, `round_lost`). This span event is a different channel from the event-bus `player_death` in §5. Consumers that count or order eliminations from spans (the agent's span ingest, §4.4) can count one event as one death.

### 3.3 controller-manager, menu, pairing daemon

| Service | Spans | Source |
|---|---|---|
| controller-manager | `controller.discover`, `controller_connected`, `spawn_controller_process` (event `controller_added_to_tracking`: `serial`, `battery`, `name`), `controller.bluetooth_health` (~1 Hz, event `bluetooth_controller_sample`; **consumed by the agent infra loop**; the event also carries `bluetooth.report_gap_p95_ms`, `bluetooth.report_gap_max_ms` and `bluetooth.connection_quality` (`good` / `fair` / `poor`) once the controller has a 10 s gap window, UC-035 BR-005), `effect_player_warning` (events `flash_start` (`color`, `duration_ms`), `vibration` (`intensity`, `duration_ms`)), `effect_player_death` (events `cancel_existing_effect`, `vibration`, `led_red`, `fade_to_black` (`duration_ms`)), `effect_winner_rainbow` (events `rainbow_start` (`duration_ms`), `rainbow_end`), `effect_countdown` (events `countdown_red` / `countdown_yellow` / `countdown_green` (`phase`, `duration_ms`), `countdown_complete`, `countdown_cancelled`), `StreamGameplayData` event `filter_updated` (`previous_count`, `new_count`), `controller.shield_effect` (one per shield layer run on a controller; attrs `controller.serial`, `shield.kind` and `effect.duration_ms` of the first layer; events `shield_blink` (`shield.kind`), `shield_end` with `shield.end_reason` = expired / cancelled; UC-040 A16, A17), `controller.identify` (one per `IdentifyController` call, child of the gRPC server span; attrs `controller.serial`, `identify.outcome` = started / not_connected / in_game / reserved / invalid, `effect.duration_ms` (the clamped duration); UC-004) | `services/controller_manager/discovery_loop.py`, `feedback_manager.py` |
| menu | `menu_publish:{event_type}`, `start_game`, `stop_lobby_music`, `stop_button_monitor`, `restart_button_monitor`, `reset_lobby_state`, `restart_lobby_music`, `admin_mode_session`, `enter_admin_mode` (event `admin_mode_entered`), `exit_admin_mode` (event `admin_mode_exited`), `admin_*` (game_mode, game_mode_change, force_start, sensitivity, battery_display, instructions, cycle_option, increase_value, decrease_value, agent_control). Admin span events: `game_mode_changed` (`old_selection`, `new_selection`), `force_start_triggered` (`game`, `player_count`), `sensitivity_changed` (`old_variant`, `new_variant`), `instructions_toggled` (`old_variant`, `new_variant`, `enabled`), `admin_option_changed` (`option`, `option_index`), `admin_value_increased` / `admin_value_decreased` (`option`, `old_variant`, `new_variant`), `agent_policy_changed` (`old_level`, `new_level`), `battery_display_triggered` | `services/menu/servicer.py`, `handlers/admin.py` |
| pairing-daemon | `bluetooth_monitor_cycle`, `pair_controller`, `process_controller`, `poll_cycle` | `services/pairing_daemon/psmove_pairing/` |

**Traceability:** FR-043, FR-074, FR-080 / UC-020 to UC-030, UC-035, UC-050 / files above / `tests/integration/test_decide_backend_e2e.py`.

## 4. Agent

### 4.1 Spans and topology

| Span | Kind / parent | Source |
|---|---|---|
| `agent.signal_received` | SERVER; remote child of the game's `game_cycle` (fallback order: `gameplay_phase`, game root, else own root; the `agent.decision` spans below keep their Link to the game root in every case, #1325); attrs `otlp.signal`, `game.id`, `game.trace_id` | `services/agent/decision/decision.go:startSignalReceivedSpan`; constant `decision/telemetry.go:34` |
| `agent.decide` | rules path: sibling of `agent.decision` under `agent.signal_received`; remote path: remote child of `game_cycle` (same fallback order as `agent.signal_received`), Link `link.kind=fire_cycle`, parent of `chat {model}` and `agent.decision` | `decision/decide_hook.go` (`emitDecideSpan`, `setDecideAttributes`, `runDecideRemote`) |
| `agent.decision` → `agent.action` | Link to the game trace (Link attrs `link.kind`, `link.trace_id`); event `feature_flag`; the cycle's flag state as attrs `agent.enabled`, `agent.objectives`, `interventions.allowed`, `policy.battery_threshold`, `policy.movement_variance_window`, `policy.max_interventions_per_minute` (`decision/decision.go:layerStateAttributes`, constants `decision/telemetry.go`; also on `agent.disabled`). A player-targeted decision (blocked or applied) carries `player.serial` (the target) and, when the agent knows it and the game is not `shadow`, `player.name` on both spans (#1295) | `decision/decision.go` (`runDecision`, `targetAttributes`) |
| `agent.disabled` | throttled kill-switch audit | `decision/decision.go:emitDisabledSpan` |
| `agent.intervention.effect` | one span per evaluated fitness signal, each a direct remote child of the `agent.decision` that dispatched the intervention, backdated to dispatch time and ended after the follow-up window (#1325; there is no intermediate effect span, so consecutive effect spans never nest). A follow-up whose game is gone emits one span with `intervention.effect_aborted=true`, also under the decision. Without a valid decision span context the span is its own root | `decision/effect.go` (`emitEffectSample`) |
| `agent.llm.retro` | own root, or child of `agent.experiment`; Link to the game; event `retro.suggestion`; attrs `agent.mode=retro`, `agent.objectives`, `interventions.allowed`. Capture-only spans carry `inference.used=none` and `inference.fallback_reason` = `no_backend_available` / `llm_not_eligible` / `agent_disabled` (kill switch off, #1270) | `decision/retro_capture.go` |
| `agent.game.summary` | own root or child of `agent.experiment` | `gamesummary/coordinator.go` (`Coordinator.OnGameEnd`) |
| `agent.experiment` | long-lived **root**; its traceparent is injected into shadow-game spawns | `experiment/registry.go:startSpanLocked` |
| `experiment.fitness`, `experiment.evaluate`, `experiment.verdict` | children of `agent.experiment` | `experiment_loop.go` (`scoreGameFitness` and the evaluate / verdict path) |
| `agent.infrastructure.decision` | per active infra cycle | `decision/infra_loop.go:emitDecisionSpan` |
| `agent.shadow_game.run` | one-shot shadow game (legacy runner and the pattern-game one-shot runner); attrs `shadow_game.run_id`, `shadow_game.mode`, `shadow_game.players`, `shadow_game.sensitivity`, `shadow_game.game_id`, `shadow_game.outcome`, `shadow_game.duration_ms`; events `controllers_reserved` (`shadow_game.serials`), `controllers_removed` (`shadow_game.removed_count`) | `gamerunner/runner.go:RunShadowGame` |
| `code_improvement.proposed`, `code_improvement.propose_attempt`, `code_improvement.promote` | proposal, promotion. `propose_attempt` carries `code_improvement.outcome` = `proposed` / `no_backend` / `undecodable` / `blocked` / `disabled` (+ `code_improvement.reason` when the kill switch flipped during the model call, #1270); `gated`, `llm_gated` and a `disabled` cycle before the call emit no span | `experiment/gate.go:117`, `experiment/proposer.go`, `promote/promote.go:424` |
| `chat {model}` | CLIENT; decide backends (claude, openai, jev) and the retro / proposer backend | `decide/backends/*/`, `inference/claude.go:Infer` |

```text
[game trace]  <mode root> ─ gameplay_phase ─┬─ players ─ player: {name} ...
                                            └─ game_cycle ─(remote parent)─> agent.signal_received
                                                                               ├─ agent.decide (rules)
                                                                               └─ agent.decision ─┬─ agent.action
                                                                                                  └─ agent.intervention.effect (one per signal)
                                               game_cycle ─(remote parent)─> agent.decide (remote) ─┬─ chat {model}
                                                                                                    └─ agent.decision ─> agent.action, agent.intervention.effect
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
| Metrics | names `game_player_accel_magnitude`, `controller_battery_level`, `controller_battery_pct`, `game_player_playstyle`, `game_player_alive`, `controller_connected`, `game_duration_seconds`, `game_active_players`, `game_active`, `game_current_mode`, `game_player_peak_accel`, `game_trace_correlation`, `game_music_tempo`, `game_effective_death_threshold`, `game_effective_warning_threshold`, `game_player_movement_variance`, `game_player_movement_variance_aggregate`, `game_player_skill_level`, `game_player_elimination_order` (`game_current_mode` is never emitted by the coordinator, so that input stays empty: Known gap tracked in #1336; the agent does not ingest `game_player_deaths_total` (removed in #1376): per-game deaths and their order come from `game_player_elimination_order` and the `player_death` span event; `game_current_mode`, `game_music_tempo` and the two threshold gauges carry no `game_id`, see #1336); `controller_info` (labels `serial`, `name`: identity only, kept process-wide as the serial → display-name view for `player.name`, never routed to a game partition and never a trigger, #1295); labels `serial`, `mode`, `game_id`, `game_kind`, `game_trace_id`, `game_trace_span_id`, `gameplay_phase_span_id`, `game_cycle_span_id`, `experiment_id`, `arm` | `services/agent/gamecontext/extract_metrics.go:13-91`; `controller_info`: `gamecontext/multiplexer.go` (`PlayerName`) |
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

**Traceability:** FR-040 to FR-049, FR-056, FR-063, FR-071, FR-072 / UC-040 to UC-045, UC-050, UC-051 / `services/agent/decision/`, `services/agent/experiment/`, `services/agent/gamecontext/` / `services/agent/decision/*_test.go` (trace_link, signal_telemetry, gameplay_phase_parent, game_cycle_parent, effect_parent), `tests/integration/test_decide_backend_e2e.py`, `tests/integration/test_experiment_cohort_loop.py`.

## 5. Game events

Published on each game's event bus (`StreamGameEvents`) and recorded as span events on the current span
(`services/game_coordinator/event_bus.py:EventBus.publish`):

| Group | Event types | Source |
|---|---|---|
| Coordinator | `game_start`, `game_start_error`, `game_error`, `game_force_ended`, `agent_intervention` | `servicer.py` (`_start_game_from_config`, `StreamGameEvents`); `game_session.py` (`_run_game_loop_async`, `force_end`); `interventions.py:_record` |
| Lifecycle | `game_starting`, `players_initialized`, `countdown_start`, `countdown_end`, `game_started`, `player_revived`, `game_ended` (`game_id`, `duration`; JoustFFA adds `outcome` = `winner` / `tie` / `force_ended` and, for `force_ended`, `reason`, #1328; the agent's shadow runner classifies a pattern game from it, UC-046 BR-016), `game_winner`, `game_tie`, `player_analytics`, `player_shielded` (`serial`, `kind` = full / partial, `seconds`, `until_ms`; UC-040 A16) | `games/base.py`, mode files |
| Teams | `team_winner`, `team_formation_start`, `team_formation_end` | `teams_base.py:354`, `random_teams.py:198,222` |
| Traitor / Werewolf | `traitor_signal_start` / `_end`, `traitor_winner`, `werewolf_intro_start` / `_end`, `werewolf_reveal`, `werewolf_winner` | `traitor.py`, `werewolf.py` |
| Zombies / Swapper | `zombie_intro_start` / `_end`, `time_announcement`, `human_converted`, `zombie_winner`, `player_swapped`, `swapper_winner` | `zombie.py`, `swapper.py` |
| 1v1 | `round_start`, `round_end`, `round_timeout`, `tournament_intro`, `match_start`, `match_end`, `tournament_champion`, `tournament_no_winner` | `fight_club.py`, `tournament.py`. **Known gap:** `tournament_champion` is never published (the match loop exits before the win check), tracked in #1337 |
| NonStop, JoustFFA | `player_death` (`serial`; FFA adds `alive_remaining`, used by the agent for the pattern-game elimination order, UC-046 BR-014), `player_respawned` (NonStop) | `nonstop_joust.py` (`_kill_player_impl`, `_respawn_player`), `ffa.py:_kill_player_impl` |

Defined in `lib/types.py` but never published: `player_revive`, `player_out`, `score_update`,
`team_eliminated`. Game-ending events for the menu: `game_ended`, `game_force_ended`, `game_error`.

**Traceability:** FR-020 to FR-028 / UC-020 to UC-027 / `services/game_coordinator/` / `tests/integration/test_full_game_lifecycle.py`, `tests/integration/test_parallel_lifecycle.py`.

## 6. Metrics

Python metrics are created through `lib/otel_metrics.py` (`Counter`, observable `Gauge`, `Histogram`; no unit
argument). A `Histogram`'s `buckets` are passed to the SDK as explicit bucket boundaries, so each histogram exports
exactly the buckets it declares (UC-035 BR-004, #1434). **158 unique Python metric names** (181 definitions: the `process_*` trio is defined per service, and
`grpc_requests_total`, `grpc_request_duration_seconds`, `audio_sounds_played_total` and
`audio_playback_errors_total` are defined in more than one service); the Go agent adds 7 `agent_*` metrics plus the
two `gen_ai.client.*` histograms, and Go connect-proxy and rust-hid emit only the `process_*` trio. The main ones per
service are below; §6.2 lists every Python metric.

| Service | Metrics (type) | Source |
|---|---|---|
| all Python | `process_cpu_seconds_total` (C), `process_resident_memory_bytes` (G), `process_threads` (G), `grpc_requests_total` (C), `grpc_request_duration_seconds` (H), `grpc_client_request_duration_seconds` (H), `grpc_client_errors_total` (C), `flag_eval_errors_total{flag_key,reason}` (C) | `services/*/metrics.py`, `lib/grpc_metrics.py`, `lib/flag_eval_visibility.py:47` |
| game-coordinator | `game_active`, `game_duration_seconds`, `game_active_players` (G, labels `game_kind, game_id, experiment_id, arm`); `game_trace_correlation` (G); `games_started_total`, `games_completed_total` (C, `mode, game_kind`); `game_player_*` (accel, peak, playstyle, alive, movement variance / zone, skill, elimination order) (G, `serial, game_id`); `game_player_deaths_total` (C, `mode, game_kind`: one increment per death on the shared kill path `BaseGameMode._kill_player`, every mode, respawn deaths included; a concurrent second kill of the same player is not counted, #1334); `game_current_mode{mode}`, `game_effective_warning_threshold`, `game_effective_death_threshold`, `game_music_tempo`, `game_sensitivity` (G, **game-wide**: no `game_id`, so concurrent real and shadow games overwrite each other; Known gap tracked in #1336; `game_current_mode` is also never set, see §6.2); `game_actual_update_frequency_hz`, `game_current_update_frequency_hz`, `game_loop_jitter_ms`, `game_loop_frame_consistency_percent`, `game_players_alive`, `game_active_controllers`, `game_filtered_controllers` (G, game-wide); `game_player_warnings_total{serial,game_mode}` (C, per warning), `game_near_death_events_total{serial,game_mode}` (C, at game end), `game_analytics_samples_total` (C); `game_accel_distribution` (H); `game_loop_frames_dropped_total` (C); `game_interventions_total{type,objective,blocked,block_reason}` (C); `game_intervention_dispatch_failures_total{type,reason}` (C, an intervention side effect that did not complete on its game's loop: `reason` = `game_loop_unavailable` or `game_loop_timeout` (not applied) or `game_loop_late` (started, outcome unknown); the best-effort agent cue is not counted; UC-040 BR-018, #1250); `game_flag_evaluations_total`, `game_flag_configuration_changes_total` (C); `game_shadow_games_blocked_total`, `game_shadow_games_preempted_total`, `game_config_changes_total{parameter}`, `game_filter_updates_total`, `game_gameplay_stream_reconnects_total` (C); `event_bus_active_subscribers` (G), `event_bus_publish_drops_total`, `event_bus_publish_errors_total` (C) | `services/game_coordinator/metrics.py` |
| controller-manager | `controller_connected`, `controller_battery_level`, `controller_battery_pct`, `controller_info{serial,name}` (one series per tracked controller: replaced on `RenameController`, removed when the controller is no longer tracked, #1295), `controller_backend_info`, `controller_adapter_info`, `controller_polling_target_hz`, `controller_polling_mode`, `controller_state_update_hz`, `controller_accel_magnitude`, `controller_color_hex`, `controller_streams_active` (G); `controller_disconnect_total`, `controller_reconnect_total`, `controller_routing_decisions_total{serial,adapter,method}`, `controller_poll_drops_total`, `controller_poll_errors_total`, `controller_chaos_faults_injected_total`, `controller_button_events_total`, `controller_stream_updates_total`, `controller_stream_frame_overruns_total`, `controller_state_cache_hits_total` / `_misses_total`, `controller_identify_requests_total{outcome}` (one per `IdentifyController` call, `outcome` as on the `controller.identify` span, UC-004) (C); `controller_input_lag_seconds` (age of the controller sample when it is sent on a game (filtered) `StreamGameplayData` stream, measured from the controller-manager poll loop's first pick-up; excludes the Bluetooth / HID hop, which has no hardware timestamp; not recorded on the dashboard's unfiltered stream or for pattern controllers; buckets up to 1 s, #1335), `controller_discovery_check_duration_seconds`, `controller_poll_batch_duration_seconds`, `controller_bluetooth_event_gap_seconds` (H); `controller_bluetooth_report_gap_p95_seconds{serial}`, `controller_bluetooth_report_gap_max_seconds{serial}` (p95 and longest gap between fresh frames over a rolling 10 s window, taken at poll pick-up with the poll loop's own lateness subtracted; the longest includes the still-open gap; a controller silent for the whole window reports the open gap and poor), `controller_connection_quality{serial}` (0 good, 1 fair, 2 poor; UC-035 BR-005; the three series are absent until the controller has delivered two fresh frames and are removed when the controller is no longer tracked) (G) | `services/controller_manager/metrics.py`, `connection_quality.py` |
| menu | `menu_player_ready`, `menu_idle_mode_active`, `menu_stream_connections_active` (G); `menu_button_presses_total`, `menu_ghost_controllers_pruned_total`, `menu_force_start_reconcile_failed_total`, `menu_button_event_errors_total{event_type}` (C) | `services/menu/metrics.py` |
| audio | `audio_sounds_played_total{result}` (`result` includes `file_not_found`, `no_channel`), `audio_playback_errors_total` (channel playback exceptions only), `audio_channel_saturation_total` (C); `audio_channels_active`, `audio_music_playing`, `audio_music_tempo` (G) | `services/audio/metrics.py` |
| pairing-daemon | `psmove_pairing_attempts_total` / `_success_total` / `_failed_total`, `psmove_pairing_adapter_selected_total{adapter}`, `psmove_pairing_polls_total` (C); `psmove_pairing_usb_controllers`, `psmove_pairing_adapter_device_count_at_selection{adapter}`, `bluetooth_device_last_seen_timestamp` (G); `bluetooth_device_rssi_dbm{serial,hci_adapter}`, `bluetooth_device_connected`, `bluetooth_adapter_connections` (G); `pairing_bluetooth_adapter_info{hci_adapter,address,manufacturer,manufacturer_id,hci_version,bus,usb_id,onboard}` (G, always 1: identity from `hciconfig -a` and sysfs, `unknown` when not readable; `onboard` = `true` for a non-USB bus or the Raspberry Pi device-tree radio; replaced when the identity changes, UC-035 step 6), `pairing_bluetooth_link_role_info{serial,hci_adapter,role}` (G, always 1: the local adapter's role on the link from `hcitool con`, `central` / `peripheral` / `unknown`; removed when the link goes), `pairing_bluetooth_adapter_hci_errors_total{hci_adapter,direction}` (C, `direction` = `rx` / `tx`: increases of the kernel's cumulative `hciconfig -a` error counters; a drop (adapter reset) counts the new value); `psmove_pairing_duration_seconds` (H) | `services/pairing_daemon/psmove_pairing/metrics.py`, `adapter_identity.py`, `bluetooth_monitor.py` |
| agent | `agent_llm_gated_total{reason}` (decide path, retro and, since #1270, the experiment proposer; decide-path reasons `llm_not_eligible`, `llm_budget_exhausted`, `unknown_backend`, `backend_unavailable`, `async_not_wired` (configuration fallbacks, counted every cycle since #1377), `decide_interval` and, since #1298, `no_interventions_allowed` (empty allow-list, no remote call, decide-contract §7.1)), `agent_evaluations_total{signal,outcome}`, `agent_decisions_total{action,blocked,block_reason}`, `agent_interventions_applied_total{action}`, `agent_intervention_writes_total{result}`, `agent_intervention_file_corruption_total{phase}` (C); `agent_intervention_effect_delta` (H); `gen_ai.client.operation.duration`, `gen_ai.client.token.usage` (H) | `services/agent/decision/metrics.go`, `actions/writer.go` (`newWritesCounter`, `newCorruptionCounter`), `decide/backends/claude/metrics.go` |

`game_duration_seconds` is the seconds since the game was started (StartGame, so it includes the countdown). The
coordinator refreshes it every 1 s while the session is RUNNING (real and shadow sessions alike), sets the final
value when the game loop ends, and removes the series when the session retires. It carries the same per-game label
tuple as `game_active` (`game_kind, game_id, experiment_id, arm`), so there is one series per live game (UC-041
BR-003; `services/game_coordinator/game_session.py` `_publish_duration_while_running`).

### 6.1 Consumers

| Consumer | Uses | Source |
|---|---|---|
| Grafana (13 dashboards, default datasource VictoriaMetrics, uid `prometheus`) | agent-operations, interventions, game-quality, game-analytics, player-insights, controller-overview, controller-maintenance, bluetooth-adapter, feature-flags, **Infrastructure & Services** (uid `joustmania-service-health`), and the Prometheus-stack dashboards prometheus-health, metrics-pipeline-comparison and presentation-pipeline (the last two also query `prometheus-direct`; all three go with that stack). #1427 deleted backend-comparison, performance-experiment, cache-performance and presentation-chaos, and merged fleet-overview, host-metrics and victoria-metrics into Infrastructure & Services. Infrastructure & Services matches service jobs by name with an optional namespace prefix (`job=~"(.+/)?agent"`), because VictoriaMetrics keeps the `service.namespace` prefix (`infrastructure/agent`, `observability/otel-collector`, `host/node`) and Prometheus strips it; its host row reads the collector `hostmetrics` receiver (`system_*`, job `host/node`). **Controller thresholds** (one definition for every dashboard): low battery = `controller_battery_level < 2` (level 0 or 1 of 5, the controller-manager's low-battery level); weak signal = `bluetooth_device_rssi_dbm < -80` (poor, the red band of every RSSI colour scale and the controller's weak-signal LED warning; [CONTROLLER_CONNECTIVITY.md](../CONTROLLER_CONNECTIVITY.md) calls -70 to -80 dBm fair and -55 dBm or better good; the dashboard colour scales turn yellow at -80 and green at -60 dBm). Every referenced app metric is recorded and every label matcher on a Python metric names a label it carries (checked by `tests/contract/test_telemetry_contract.py`, which also checks that its query walk finds at least one query per dashboard file and no fewer queries than a raw scan of non-empty `"expr"` fields). The Infrastructure & Services "gRPC client errors" panels read `grpc_client_errors_total` (failed outbound calls and stream setups from the Python services, by callee `service`, `method`, `code`; #1353); mid-stream and non-Python client errors are not counted yet (#1426). No dashboard queries Loki or Jaeger. | `services/grafana/dashboards/` |
| Dashboard SPA | Jaeger `service=agent operation=agent.decide` (tags `decide.*` incl. the `decide.answers` shape, `gen_ai.request.model`, `gen_ai.response.model`, `game.id`, `game.kind`, `decision.*`, `player.serial` / `player.name` on `agent.decision` for the Decisions row target (#1295); a child is a chat span when its name starts with `chat ` or `gen_ai.operation.name=chat`); Loki agent log lines (§7); VictoriaMetrics `sum(agent_intervention_writes_total) by (result)` | `services/dashboard/src/agent.ts`, `experiments.ts` |
| Agent | §4.4 | |
| Integration tests | span names `agent.decide`, `agent.signal_received`, `agent.decision`, `chat {model}`, `agent.disabled`; `decide.*` tags; event `gen_ai.evaluation.result` | `tests/integration/test_decide_backend_e2e.py` |
| Demo validation harness | span chain `agent.signal_received` → `agent.decision` → `agent.action` per game; Prometheus queries | `tools/demo/validation_harness.py` |
| Talk pre-flight | Jaeger `service=agent operation=agent.decide`, tags `decide.backend`, `decide.outcome`, `decide.fallback_reason`; flag **variant names** of the stage baseline (`ambient`, `default`, `haiku`, `real_only`, ...) | `scripts/talk-preflight.sh` |

96 of the 158 Python metric names have no consumer in a dashboard, the agent, the dashboard SPA, the demo
harness or a script (all `menu_*`, `psmove_*`, `python_hid_*`, `audio_*` and others); they are kept for ad-hoc
analysis. `tests/contract/test_telemetry_contract.py` (#1357) checks the agent's and the dashboards' names against the producers.

### 6.2 Python metric catalog

Generated from the `Counter` / `Gauge` / `Histogram` definitions in `services/*/metrics.py`, `lib/grpc_metrics.py`
and `lib/flag_eval_visibility.py` at 84c3ba15. **Emitted** = some non-test code in the defining service records it
(`.inc` / `.set` / `.observe` / `.labels`); **Consumers** = the directories whose dashboards (`grafana`), agent extractor (`agent`), SPA (`dashboard`), demo harness (`tools`) or scripts
(`scripts`) name it. Types: counter (C), gauge (G, observable), histogram (H). 16 names are defined but never
recorded by any service; they are listed so a consumer does not trust them.

| Metric | Type | Labels | Defined in | Emitted | Consumers |
|---|---|---|---|---|---|
| `flag_eval_errors_total` | counter | `flag_key`, `reason` | lib (all Python) | yes | grafana |
| `grpc_request_duration_seconds` | histogram | `method` | audio, controller-manager, game-coordinator, menu | emitted by menu; defined but never recorded in game-coordinator, audio, controller-manager | (none) |
| `grpc_requests_total` | counter | `method`, `status` | audio, controller-manager, game-coordinator, menu | emitted by menu; defined but never recorded in game-coordinator, audio, controller-manager | (none) |
| `process_cpu_seconds_total` | counter | (none) | audio, controller-manager, game-coordinator, menu, pairing-daemon, python-hid | yes (each service) | grafana |
| `process_resident_memory_bytes` | gauge | (none) | audio, controller-manager, game-coordinator, menu, pairing-daemon, python-hid | yes (each service) | grafana |
| `process_threads` | gauge | (none) | audio, controller-manager, game-coordinator, menu, pairing-daemon, python-hid | yes (each service) | grafana |
| `grpc_client_errors_total` | counter | `service`, `method`, `code` | lib (all Python gRPC clients) | yes | grafana |
| `grpc_client_request_duration_seconds` | histogram | `service`, `method` | lib (all Python gRPC clients) | yes | grafana |
| `event_bus_active_subscribers` | gauge | (none) | game-coordinator | yes | (none) |
| `event_bus_publish_drops_total` | counter | `event_type` | game-coordinator | yes | (none) |
| `event_bus_publish_errors_total` | counter | `event_type` | game-coordinator | yes | (none) |
| `game_accel_distribution` | histogram | `game_mode` | game-coordinator | yes | grafana |
| `game_active_controllers` | gauge | (none) | game-coordinator | yes | (none) |
| `game_active_players` | gauge | `game_kind`, `game_id`, `experiment_id`, `arm` | game-coordinator | yes | agent, grafana |
| `game_active` | gauge | `game_kind`, `game_id`, `experiment_id`, `arm` | game-coordinator | yes | agent, grafana |
| `game_actual_update_frequency_hz` | gauge | (none) | game-coordinator | yes | grafana |
| `game_analytics_replay_bytes` | histogram | `game_mode` | game-coordinator | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `game_analytics_samples_total` | counter | `game_mode` | game-coordinator | yes | grafana |
| `game_config_changes_total` | counter | `parameter` | game-coordinator | yes | (none) |
| `game_countdown_accuracy_seconds` | histogram | (none) | game-coordinator | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `game_current_mode` | gauge | `mode` | game-coordinator | **no**: defined, never recorded. Known gap tracked in #1336 | agent |
| `game_current_update_frequency_hz` | gauge | (none) | game-coordinator | yes | grafana |
| `game_duration_seconds` | gauge | `game_kind`, `game_id`, `experiment_id`, `arm` | game-coordinator | yes | agent, grafana, tools |
| `game_effective_death_threshold` | gauge | (none) | game-coordinator | yes | agent, grafana |
| `game_effective_warning_threshold` | gauge | (none) | game-coordinator | yes | agent, grafana |
| `game_filter_updates_total` | counter | `game_mode` | game-coordinator | yes | (none) |
| `game_filtered_controllers` | gauge | (none) | game-coordinator | yes | (none) |
| `game_flag_configuration_changes_total` | counter | (none) | game-coordinator | yes | grafana |
| `game_flag_evaluations_total` | counter | `flag_key` | game-coordinator | yes | grafana |
| `game_gameplay_stream_reconnects_total` | counter | `game_mode` | game-coordinator | yes | (none) |
| `game_intervention_dispatch_failures_total` | counter | `type`, `reason` | game-coordinator | yes | (none) |
| `game_interventions_total` | counter | `type`, `objective`, `blocked`, `block_reason` | game-coordinator | yes | grafana, tools |
| `game_loop_frame_consistency_percent` | gauge | (none) | game-coordinator | yes | grafana |
| `game_loop_frames_dropped_total` | counter | `mode` | game-coordinator | yes | grafana |
| `game_loop_jitter_ms` | gauge | (none) | game-coordinator | yes | grafana |
| `game_music_tempo` | gauge | (none) | game-coordinator | yes | agent, grafana |
| `game_near_death_events_total` | counter | `serial`, `game_mode` | game-coordinator | yes | grafana |
| `game_player_accel_magnitude` | gauge | `serial`, `game_id` | game-coordinator | yes | agent, grafana |
| `game_player_alive` | gauge | `serial`, `game_id` | game-coordinator | yes | agent, grafana, tools |
| `game_player_deaths_total` | counter | `mode`, `game_kind` | game-coordinator | yes (#1376) | (none) |
| `game_player_elimination_order` | gauge | `serial`, `game_id` | game-coordinator | yes | agent |
| `game_player_kills_total` | counter | `mode`, `serial` | game-coordinator | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `game_player_movement_variance_aggregate` | gauge | `serial`, `game_id` | game-coordinator | yes | agent |
| `game_player_movement_variance` | gauge | `serial`, `game_id` | game-coordinator | yes | agent, tools |
| `game_player_movement_zone` | gauge | `serial`, `game_id` | game-coordinator | yes | grafana |
| `game_player_peak_accel` | gauge | `serial`, `game_id` | game-coordinator | yes | agent, grafana |
| `game_player_playstyle` | gauge | `serial`, `game_id` | game-coordinator | yes | agent, grafana |
| `game_player_respawns_total` | counter | `serial` | game-coordinator | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `game_player_skill_level` | gauge | `serial`, `game_id` | game-coordinator | yes | agent, tools |
| `game_player_warnings_total` | counter | `serial`, `game_mode` | game-coordinator | yes | grafana |
| `game_players_alive` | gauge | (none) | game-coordinator | yes | grafana |
| `game_sensitivity` | gauge | (none) | game-coordinator | yes | grafana |
| `game_shadow_games_blocked_total` | counter | (none) | game-coordinator | yes | (none) |
| `game_shadow_games_preempted_total` | counter | (none) | game-coordinator | yes | (none) |
| `game_trace_correlation` | gauge | `game_kind`, `game_id`, `game_trace_id`, `game_trace_span_id`, `gameplay_phase_span_id`, `game_cycle_span_id` | game-coordinator | yes | agent |
| `game_winner_announcement_delay_seconds` | histogram | (none) | game-coordinator | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `games_completed_total` | counter | `mode`, `game_kind` | game-coordinator | yes | (none) |
| `games_started_total` | counter | `mode`, `game_kind` | game-coordinator | yes | grafana |
| `active_controllers_total` | gauge | (none) | controller-manager | yes | (none) |
| `bluetooth_adapter_count` | gauge | (none) | controller-manager | yes | (none) |
| `controller_accel_magnitude` | gauge | `serial` | controller-manager | yes | grafana |
| `controller_accel_x` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_accel_y` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_accel_z` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_adapter_info` | gauge | `serial`, `adapter` | controller-manager | yes | grafana |
| `controller_adaptive_polling_active` | gauge | (none) | controller-manager | yes | (none) |
| `controller_adaptive_polling_idle` | gauge | (none) | controller-manager | yes | (none) |
| `controller_backend_info` | gauge | `serial`, `backend` | controller-manager | yes | grafana |
| `controller_battery_check_duration_seconds` | histogram | (none) | controller-manager | yes | (none) |
| `controller_battery_checks_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_battery_level` | gauge | `serial` | controller-manager | yes | agent, grafana |
| `controller_battery_pct` | gauge | `serial` | controller-manager | yes | agent |
| `controller_bluetooth_dropped_events_ratio_window` | gauge | (none) | controller-manager | yes | (none) |
| `controller_bluetooth_dropped_events_ratio` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_bluetooth_event_gap_seconds` | histogram | (none) | controller-manager | yes | (none) |
| `controller_bluetooth_movement_update_hz` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_bluetooth_report_gap_max_seconds` | gauge | `serial` | controller-manager | yes | grafana |
| `controller_bluetooth_report_gap_p95_seconds` | gauge | `serial` | controller-manager | yes | grafana |
| `controller_button_events_total` | counter | `serial`, `button`, `action` | controller-manager | yes | grafana |
| `controller_chaos_faults_injected_total` | counter | `fault`, `serial` | controller-manager | yes | (none) |
| `controller_color_b` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_color_g` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_color_hex` | gauge | `serial` | controller-manager | yes | grafana |
| `controller_color_r` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_connected` | gauge | `serial` | controller-manager | yes | agent, grafana |
| `controller_connection_quality` | gauge | `serial` | controller-manager | yes | grafana |
| `controller_disconnect_total` | counter | `serial` | controller-manager | yes | grafana |
| `controller_discovery_check_duration_seconds` | histogram | (none) | controller-manager | yes | (none) |
| `controller_discovery_checks_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_discovery_full_enumerate_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_discovery_verify_only_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_info` | gauge | `serial`, `name` | controller-manager | yes | agent, grafana |
| `controller_identify_requests_total` | counter | `outcome` | controller-manager | yes | (none) |
| `controller_input_lag_seconds` | histogram | `serial` | controller-manager | yes (#1376; game streams only) | (none) |
| `controller_led_batch_updates_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_led_controllers_updated_per_batch` | histogram | (none) | controller-manager | yes | (none) |
| `controller_object_pool_utilization` | gauge | `pool_type` | controller-manager | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `controller_poll_batch_duration_seconds` | histogram | (none) | controller-manager | yes | (none) |
| `controller_poll_batch_size` | histogram | (none) | controller-manager | yes | (none) |
| `controller_poll_drops_total` | counter | `serial` | controller-manager | yes | grafana |
| `controller_poll_errors_total` | counter | `serial` | controller-manager | yes | grafana |
| `controller_polling_mode` | gauge | (none) | controller-manager | yes | grafana |
| `controller_polling_target_hz` | gauge | (none) | controller-manager | yes | grafana |
| `controller_reconnect_total` | counter | `serial` | controller-manager | yes | grafana |
| `controller_routing_decisions_total` | counter | `serial`, `adapter`, `method` | controller-manager | yes | (none) |
| `controller_state_cache_hits_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_state_cache_misses_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_state_update_hz` | gauge | `serial` | controller-manager | yes | (none) |
| `controller_stream_commands_total` | counter | `command_type` | controller-manager | yes | (none) |
| `controller_stream_current_frequency_hz` | gauge | (none) | controller-manager | yes | (none) |
| `controller_stream_frame_overruns_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_stream_frequency_changes_total` | counter | (none) | controller-manager | yes | (none) |
| `controller_stream_updates_total` | counter | `stream_type` | controller-manager | yes | (none) |
| `controller_streamed_per_frame` | histogram | (none) | controller-manager | yes | (none) |
| `controller_streams_active` | gauge | (none) | controller-manager | yes | grafana |
| `menu_button_event_errors_total` | counter | `event_type` | menu | yes | (none) |
| `menu_button_frames_processed_total` | counter | (none) | menu | yes | (none) |
| `menu_button_presses_total` | counter | `button`, `action` | menu | yes | (none) |
| `menu_force_start_reconcile_failed_total` | counter | (none) | menu | yes | (none) |
| `menu_game_event_stream_reconnects_total` | counter | (none) | menu | yes | (none) |
| `menu_ghost_controllers_pruned_total` | counter | (none) | menu | yes | (none) |
| `menu_idle_controllers_count` | gauge | (none) | menu | yes | (none) |
| `menu_idle_mode_active` | gauge | (none) | menu | yes | (none) |
| `menu_idle_mode_entries_total` | counter | (none) | menu | yes | (none) |
| `menu_idle_mode_wake_total` | counter | (none) | menu | yes | (none) |
| `menu_lobby_updates_total` | counter | (none) | menu | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `menu_player_ready` | gauge | `serial` | menu | yes | (none) |
| `menu_sentinel_controllers_count` | gauge | (none) | menu | yes | (none) |
| `menu_stream_connections_active` | gauge | (none) | menu | yes | (none) |
| `menu_stream_disconnections_total` | counter | (none) | menu | yes | (none) |
| `menu_stream_events_published_total` | counter | `event_type` | menu | yes | (none) |
| `menu_stream_publish_drops_total` | counter | `event_type` | menu | yes | (none) |
| `audio_channel_saturation_total` | counter | (none) | audio | yes | (none) |
| `audio_channels_active` | gauge | (none) | audio | yes | (none) |
| `audio_music_playing` | gauge | (none) | audio | yes | (none) |
| `audio_music_tempo` | gauge | (none) | audio | yes | (none) |
| `audio_playback_errors_total` | counter | (none) | audio, game-coordinator | emitted by audio; defined but never recorded in game-coordinator (dead duplicate definitions; cleanup tracked in #1322) | (none) |
| `audio_sounds_played_total` | counter | game-coordinator: `sound_type`; audio: `result` | audio, game-coordinator | emitted by audio; defined but never recorded in game-coordinator (dead duplicate definitions; cleanup tracked in #1322) | (none) |
| `bluetooth_adapter_connections` | gauge | `hci_adapter` | pairing-daemon | yes | grafana |
| `bluetooth_device_connected` | gauge | `serial`, `hci_adapter` | pairing-daemon | yes | (none) |
| `bluetooth_device_last_seen_timestamp` | gauge | `serial`, `hci_adapter` | pairing-daemon | yes | (none) |
| `bluetooth_device_rssi_dbm` | gauge | `serial`, `hci_adapter` | pairing-daemon | yes | grafana |
| `pairing_bluetooth_adapter_hci_errors_total` | counter | `hci_adapter`, `direction` | pairing-daemon | yes | grafana |
| `pairing_bluetooth_adapter_info` | gauge | `hci_adapter`, `address`, `manufacturer`, `manufacturer_id`, `hci_version`, `bus`, `usb_id`, `onboard` | pairing-daemon | yes | grafana |
| `pairing_bluetooth_link_role_info` | gauge | `serial`, `hci_adapter`, `role` | pairing-daemon | yes | grafana |
| `psmove_pairing_adapter_device_count_at_selection` | gauge | `adapter` | pairing-daemon | yes | (none) |
| `psmove_pairing_adapter_selected_total` | counter | `adapter` | pairing-daemon | yes | (none) |
| `psmove_pairing_attempts_total` | counter | (none) | pairing-daemon | yes | (none) |
| `psmove_pairing_duration_seconds` | histogram | (none) | pairing-daemon | yes | (none) |
| `psmove_pairing_failed_total` | counter | (none) | pairing-daemon | yes | (none) |
| `psmove_pairing_polls_total` | counter | (none) | pairing-daemon | yes | (none) |
| `psmove_pairing_success_total` | counter | (none) | pairing-daemon | yes | (none) |
| `psmove_pairing_usb_controllers` | gauge | (none) | pairing-daemon | yes | (none) |
| `python_hid_active_streams` | gauge | (none) | python-hid | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `python_hid_devices_open` | gauge | (none) | python-hid | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `python_hid_discover_total` | counter | (none) | python-hid | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `python_hid_open_total` | counter | `success` | python-hid | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `python_hid_poll_duration_seconds` | histogram | (none) | python-hid | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |
| `python_hid_sensor_data_total` | counter | (none) | python-hid | **no**: defined, never recorded (dead definition; cleanup tracked in #1322) | (none) |

**Traceability:** FR-081, FR-084, NFR-022 / UC-035 / metrics files above / `services/*/tests/` (metric patches), `tests/integration/test_decide_backend_e2e.py`.

## 7. Log lines that are part of the contract

Agent logs are slog logfmt; consumers match the message **and parse the listed keys**. Renaming a message or a
key breaks them.

| Message | Keys consumers parse | Consumers | Emitted at |
|---|---|---|---|
| `agent.evaluate` | `session_id`, `game_mode`, `decision_backend`, `battery_threshold`, `variance_window`, `max_per_minute` (also emitted: `player_count`, `duration`) | dashboard Agent tab (Loki `{service_name="agent"}`, `services/dashboard/src/agent.ts`); `tools/demo/validation_harness.py:397` (`decision_backend=`); `tests/integration/test_experiment_cohort_loop.py:133-136` (`decision_backend=rules`) | `services/agent/decision/decision.go:551-560` |
| `agent.decision_blocked` | `intervention`, `reason` (also: `blocked`, `target_serial`, `decision_reason`, `allowed`) | dashboard Agent tab (feed rows; its blocked tallies come from `agent_decisions_total`, the lines only as fallback, #1326); `tools/demo/validation_harness.py:398` | `services/agent/decision/decision.go:810-816`; a `rate_limit` refusal that repeats while the budget is unchanged is not logged here, only as debug `agent.decision_rate_limit_repeat` (`intervention`, `target_serial`; UC-040 A21, #1326) |
| `agent.decide.backend_misconfigured` | `state` (`entered` WARN with `reason`; `cleared` INFO with `previous_reason`), `session_id`, `backend` | operators (Loki); `services/agent/decision/decide_fallback_quiet_test.go` | `services/agent/decision/decide_hook.go` (`noteMisconfigured`); once per state change per game, #1350 |
| `agent.rollout_expand`, `agent.rollout_rollback`, `agent.rollout_rollback_recommended` | message only (they carry `dry_run`, #1375) | dashboard Agent tab | `services/agent/decision/infra_loop.go:570-578` |
| `experiment.game_assigned` | `experiment_id`, `game_id`, `arm`, `pair_index` | dashboard Experiments tab (`services/dashboard/src/experiments.ts`) | `services/agent/experiment/registry.go:1107` |
| `experiment.game_concluded` | `experiment_id`, `game_id`, `arm`, `fitness` | Experiments tab; `tests/integration/test_experiment_cohort_loop.py:461` (`fitness=`, `arm=control`, `arm=experimental`) | `registry.go:1223` |
| `experiment.concluded` | `experiment_id`, `outcome` | Experiments tab; cohort-loop test | `registry.go:1431` |
| `experiment.torn_down` | `experiment_id`, `status`, `reason` | Experiments tab; cohort-loop test | `registry.go:1586` |
| `experiment.declared`, `code_improvement.promoted`, `experiment.promote_failed`, absence of `panic:` | message only | `tests/integration/test_experiment_cohort_loop.py` | `registry.go:593,1482`, `promote/promote.go:475` |
| `Proposer/retro inference backend selected` | `backend` (`backend=stub` asserted) | `tests/integration/test_experiment_cohort_loop.py:131-132` | `services/agent/main.go:570` |
| `agent.llm.system_prompt`, `agent.llm.retro_response`, `agent.llm.retro_captured`, `agent.llm.retro_failed` | prompt SHA, response | prompt resolution by hand | `decision/prompt_fingerprint.go`, `decision/retro_capture.go` |
| `Agent intervention writes gated by LIVE interventions_enabled flag` | message only | runbooks | `services/agent/main.go:150` |

Agent logs reach Loki through `filelog/docker` with `service_name` from the compose label; they carry no
trace id (D-T9).

**Traceability:** FR-082, FR-084 / UC-035, UC-043, UC-070 / files above / `tests/integration/test_experiment_cohort_loop.py`.

## 8. Naming rules

Rules (from `.claude/rules/development.md`): metric names prefixed with the service (`game_`, `controller_`,
`menu_`, `audio_`, `agent_`), counters end in `_total`, histograms in `_seconds` / `_bytes` (or a base unit),
span names in dot notation for new spans, attributes namespaced as in §2.1.

Known violations on `main` (do not copy them; fix opportunistically, D-T6): gauge `active_controllers_total`;
`games_*`, `bluetooth_*`, `event_bus_*`, `psmove_*` and `grpc_requests_total` / `grpc_request_duration_seconds`
without a service prefix; `bluetooth_device_last_seen_timestamp` without a unit suffix; gauges `menu_idle_controllers_count`
and `menu_sentinel_controllers_count`, whose `_count` suffix collides with the histogram `_count` series;
`audio_sounds_played_total` and `audio_playback_errors_total` are also **defined** in game-coordinator with different
labels, but only the audio service records them (the coordinator copies are dead definitions, cleanup tracked in
#1322); histograms
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
