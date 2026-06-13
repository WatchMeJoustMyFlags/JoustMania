"""
OTEL Push Metrics for Game Coordinator (Issue #103).

Tracks game state, player performance, audio playback, and game quality metrics.
Uses OTLP push at 100ms intervals for real-time dashboard updates.
"""

from contextlib import suppress

from lib.otel_metrics import Counter, Gauge, Histogram

# Game state metrics
#
# Multi-session (#775): lifecycle metrics carry a ``game_kind`` label
# ("primary" / "shadow") so shadow games are observable in their own series
# instead of being invisible or clobbering the primary game's series. The
# primary (menu-driven) session reports game_kind="primary"; concurrent
# agent/shadow sessions report game_kind="shadow".
#
# Experiment attribution (#975, epic #982): the per-live-game GAUGES below carry
# experiment_id/arm. This is safe precisely because they are gauges keyed on the
# already-unbounded game_id and SET per live game — the experiment labels add no
# permanent series; they come and go with the game's game_id and are cleared at
# game end. The cumulative COUNTERS (games_started_total, games_completed_total)
# deliberately do NOT carry experiment_id/arm: a label on a cumulative counter
# creates one permanent series per experiment value, forever (TSDB blow-up), which
# violates the §4.2 cardinality discipline. Per-experiment counts come from the
# span attributes (experiment.id/experiment.arm) plus #979's dedicated game-end
# metric. The per-frame metrics (game_player_accel_magnitude, etc.) carry none of
# this. The gauge labels are "" for a non-experiment game.
active_game = Gauge(
    "game_active",
    "Whether a game is currently running (0=no, 1=yes)",
    ["game_kind", "game_id", "experiment_id", "arm"],
)

current_game_mode = Gauge(
    "game_current_mode",
    "Current game mode",
    ["mode"],  # 'ffa', 'teams_simple', 'teams_random', 'nonstop_joust', 'none'
)

game_duration_seconds = Gauge(
    "game_duration_seconds",
    "Duration of current game in seconds",
    ["game_kind", "game_id", "experiment_id", "arm"],
)

games_started_total = Counter(
    "games_started_total",
    "Total number of games started",
    ["mode", "game_kind"],
)

games_completed_total = Counter(
    "games_completed_total",
    "Total number of games completed",
    ["mode", "game_kind"],
)

# Shadow game governance (#837).
# Shadow starts rejected by the resource gate while a real (menu-origin) game is
# live and policy=block.
shadow_games_blocked_total = Counter(
    "game_shadow_games_blocked_total",
    "Total shadow game starts rejected because a real game is running (policy=block)",
)

# Shadow games force-ended because a real game started and preempted them.
shadow_games_preempted_total = Counter(
    "game_shadow_games_preempted_total",
    "Total shadow games force-ended (preempted) when a real game started",
)

# Player metrics
active_players = Gauge(
    "game_active_players",
    "Number of players currently in game",
    ["game_kind", "game_id", "experiment_id", "arm"],
)

players_alive = Gauge("game_players_alive", "Number of players currently alive")

player_deaths_total = Counter("game_player_deaths_total", "Total player deaths", ["mode", "serial", "game_id"])

player_kills_total = Counter("game_player_kills_total", "Total player kills", ["mode", "serial"])

player_respawns_total = Counter("game_player_respawns_total", "Total player respawns (Nonstop Joust only)", ["serial"])


# Audio playback metrics (Phase 29)
audio_sounds_played_total = Counter(
    "audio_sounds_played_total",
    "Total sounds played",
    ["sound_type"],  # 'countdown', 'start', 'death', 'victory', 'respawn', 'join'
)

audio_playback_errors_total = Counter("audio_playback_errors_total", "Total audio playback errors")

# Game quality metrics
countdown_accuracy_seconds = Histogram(
    "game_countdown_accuracy_seconds",
    "Accuracy of countdown timing",
    buckets=[0.001, 0.005, 0.010, 0.020, 0.050, 0.100],
)

winner_announcement_delay_seconds = Histogram(
    "game_winner_announcement_delay_seconds",
    "Time from last death to winner announcement",
    buckets=[0.1, 0.2, 0.5, 1.0, 2.0, 5.0],
)

# System metrics
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")

process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")

process_threads = Gauge("process_threads", "Number of active threads")

# gRPC metrics
grpc_requests_total = Counter("grpc_requests_total", "Total gRPC requests received", ["method", "status"])

grpc_request_duration_seconds = Histogram(
    "grpc_request_duration_seconds",
    "gRPC request duration",
    ["method"],
    buckets=[0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500, 1.0],
)

# Music tempo metric (Phase 70)
music_tempo = Gauge(
    "game_music_tempo",
    "Current music playback speed (1.0=slow, 1.3=fast, 0=no game)",
)

# Sensitivity and threshold metrics (Phase 80)
game_sensitivity = Gauge(
    "game_sensitivity",
    "Current game sensitivity level (0=ultra_slow, 1=slow, 2=medium, 3=fast, 4=ultra_fast)",
)

effective_warning_threshold = Gauge(
    "game_effective_warning_threshold",
    "Current effective warning threshold after LERP (g-force)",
)

effective_death_threshold = Gauge(
    "game_effective_death_threshold",
    "Current effective death threshold after LERP (g-force)",
)

# Runtime metrics (Phase 43)
actual_update_frequency_hz = Gauge(
    "game_actual_update_frequency_hz", "Actual measured update frequency during gameplay"
)

config_changes_total = Counter(
    "game_config_changes_total",
    "Total number of configuration changes",
    ["parameter"],  # 'update_frequency_hz', 'poll_drop_threshold', etc.
)

# Feature flag metrics (Phase 44)
flag_evaluations_total = Counter(
    "game_flag_evaluations_total",
    "Total number of feature flag evaluations",
    ["flag_key"],  # 'update_frequency_hz', 'poll_drop_threshold', etc.
)

flag_configuration_changes_total = Counter(
    "game_flag_configuration_changes_total",
    "Total number of PROVIDER_CONFIGURATION_CHANGED events received",
)

current_update_frequency_hz = Gauge(
    "game_current_update_frequency_hz",
    "Current configured update frequency from feature flags (Hz)",
)

# Frame consistency metrics (Issue #183)
game_loop_frame_consistency_percent = Gauge(
    "game_loop_frame_consistency_percent",
    "Percentage of frames within target timing (within 50% of target frame time)",
)

game_loop_jitter_ms = Gauge(
    "game_loop_jitter_ms",
    "Standard deviation of frame times in milliseconds",
)

game_loop_frames_dropped_total = Counter(
    "game_loop_frames_dropped_total",
    "Total number of frames that exceeded 2x the target frame time",
    ["mode"],
)

# Adaptive controller filtering metrics (Phase 45)
filtered_controllers = Gauge("game_filtered_controllers", "Number of controllers currently filtered out (dead players)")

filter_updates_total = Counter(
    "game_filter_updates_total",
    "Total number of controller filter updates sent",
    ["game_mode"],  # Track per game mode
)

active_controllers = Gauge("game_active_controllers", "Number of controllers currently being monitored (alive players)")

# Stream reconnection metrics (Issue #330)
gameplay_stream_reconnects_total = Counter(
    "game_gameplay_stream_reconnects_total",
    "Total gameplay stream reconnection attempts",
    ["game_mode"],
)

# Controller analytics metrics (Phase XX - Analytics)
# Real-time gauges (updated every ~1 second during gameplay)
player_accel_magnitude = Gauge(
    "game_player_accel_magnitude",
    "Current acceleration magnitude for player (g-force)",
    ["serial", "game_id"],
)

player_movement_zone = Gauge(
    "game_player_movement_zone",
    "Current movement zone (0=still, 1=active, 2=warning, 3=danger)",
    ["serial", "game_id"],
)

player_peak_accel = Gauge(
    "game_player_peak_accel",
    "Peak acceleration magnitude for player in current game (g-force)",
    ["serial", "game_id"],
)

player_playstyle = Gauge(
    "game_player_playstyle",
    "Player playstyle classification (0=calm, 1=balanced, 2=active, 3=aggressive)",
    ["serial", "game_id"],
)

player_alive = Gauge(
    "game_player_alive",
    "Player alive status (1=alive, 0=dead)",
    ["serial", "game_id"],
)

# Intervention observability metrics (#730, closes gaps from #722 §7).
# Continuous per-player signals the intervention rules engine (#726) and the
# agent fitness functions (#731) read as PromQL instant queries.
player_movement_variance = Gauge(
    "game_player_movement_variance",
    "Rolling-window variance of acceleration magnitude (g^2). "
    "Near zero indicates a player gaming the EMA by holding still.",
    ["serial", "game_id"],
)

player_skill_level = Gauge(
    "game_player_skill_level",
    "Derived player skill level in [0.0, 1.0] (blend of playstyle control and "
    "motion smoothness). Complements game_player_playstyle.",
    ["serial", "game_id"],
)

player_elimination_order = Gauge(
    "game_player_elimination_order",
    "Order in which a player was eliminated (1=first out). Set on elimination; "
    "makes session.elimination_sequence queryable per game.",
    ["serial", "game_id"],
)

# Counters (incremented on events)
near_death_events_total = Counter(
    "game_near_death_events_total",
    "Total near-death events (raw > threshold but EMA saved player)",
    ["serial", "game_mode"],
)

player_warnings_total = Counter(
    "game_player_warnings_total",
    "Total warning events triggered",
    ["serial", "game_mode"],
)

# Histogram for distribution analysis
accel_distribution = Histogram(
    "game_accel_distribution",
    "Distribution of acceleration magnitudes during gameplay",
    ["game_mode"],
    buckets=[0.5, 1.0, 1.1, 1.3, 1.5, 1.8, 2.0, 2.5, 3.0, 4.0, 5.0],
)

# Per-game summary metrics (set at end of game)
game_analytics_samples_total = Counter(
    "game_analytics_samples_total",
    "Total analytics samples recorded",
    ["game_mode"],
)

game_analytics_replay_bytes = Histogram(
    "game_analytics_replay_bytes",
    "Size of replay data stored in bytes",
    ["game_mode"],
    buckets=[10000, 50000, 100000, 200000, 500000, 1000000],
)


def clear_player_analytics(serial: str, game_id: str = "") -> None:
    """
    Clear analytics metrics for a specific player.

    Only used for targeted cleanup (e.g., controller disconnect during game).
    Normal player death sets player_alive=0 instead, letting dashboard queries
    filter dead players via game_player_alive==1 in template variables.
    Bulk cleanup at game end uses clear_all_player_analytics().

    The per-player gauges carry both a ``serial`` and a ``game_id`` label (#845),
    so removal must supply the same tuple the emitter set; ``game_id`` is the
    ending session's id (mirrors how peak_accel / elimination_order are scoped).
    """
    with suppress(KeyError, ValueError):
        player_accel_magnitude.remove(serial, game_id)

    with suppress(KeyError, ValueError):
        player_movement_zone.remove(serial, game_id)

    with suppress(KeyError, ValueError):
        player_playstyle.remove(serial, game_id)

    with suppress(KeyError, ValueError):
        player_movement_variance.remove(serial, game_id)

    with suppress(KeyError, ValueError):
        player_skill_level.remove(serial, game_id)

    with suppress(KeyError, ValueError):
        player_alive.remove(serial, game_id)

    # peak_accel and elimination_order also carry serial + game_id labels.
    if game_id:
        with suppress(KeyError, ValueError):
            player_peak_accel.remove(serial, game_id)

        with suppress(KeyError, ValueError):
            player_elimination_order.remove(serial, game_id)


def clear_session_player_analytics(
    serials: list[str],
    game_id: str = "",
    reset_global_gauges: bool = True,
) -> None:
    """
    Clear analytics metrics for a single game session at game end (#775).

    Multi-session-safe replacement for ``clear_all_player_analytics()``: removes
    only the gauges belonging to ``serials`` (the ending session's players)
    rather than globally wiping every player gauge. With two concurrent games
    this prevents the first game to end from erasing the live game's dashboard.

    The game-state gauges (music_tempo, game_sensitivity, effective_*_threshold)
    are genuinely single-game/primary concepts, so they are reset to 0 only when
    ``reset_global_gauges`` is True (i.e. the PRIMARY session ending). A shadow
    session ending must not zero them out from under the primary game.

    Args:
        serials: Controller serials of the ending session's players.
        game_id: The ending session's game_id (for serial+game_id gauges).
        reset_global_gauges: Reset the single-game state gauges (primary only).
    """
    for serial in serials:
        clear_player_analytics(serial, game_id)

    if reset_global_gauges:
        music_tempo.set(0)
        game_sensitivity.set(0)
        effective_warning_threshold.set(0)
        effective_death_threshold.set(0)


def clear_all_player_analytics() -> None:
    """
    Clear all player analytics metrics at game end.

    This is the single cleanup point for the game lifecycle, removing all
    label combinations so the Player Insights dashboard shows no data
    between games. Per-death metric removal was eliminated (Issue #458)
    to prevent dashboard flicker during game transitions.

    DEPRECATED for multi-session use (#775): does a global wipe that clobbers
    concurrent sessions. Prefer ``clear_session_player_analytics()``. Retained
    for any callers that genuinely want a full reset.
    """
    player_accel_magnitude._metrics.clear()
    player_accel_magnitude._values.clear()
    player_movement_zone._metrics.clear()
    player_movement_zone._values.clear()
    player_playstyle._metrics.clear()
    player_playstyle._values.clear()
    player_movement_variance._metrics.clear()
    player_movement_variance._values.clear()
    player_skill_level._metrics.clear()
    player_skill_level._values.clear()
    player_peak_accel._metrics.clear()
    player_peak_accel._values.clear()
    player_elimination_order._metrics.clear()
    player_elimination_order._values.clear()
    player_alive._metrics.clear()
    player_alive._values.clear()
    # Reset game state gauges to 0 to indicate no game running
    music_tempo.set(0)
    game_sensitivity.set(0)
    effective_warning_threshold.set(0)
    effective_death_threshold.set(0)


# Agent intervention audit metric (#730, #722 §7).
# Incremented by the flag-application layer in a later PR (PR C) for every agent
# intervention attempt — applied or blocked. Also feeds the weighted rate limiter
# (#722 §5) and the #731 fitness functions. Defined here now so the metric name
# exists in the registry; zero usages in this PR is intentional.
#   type:         intervention identifier (e.g. 'adjust_player_sensitivity', 'grant_shield')
#   objective:    active session objective ('endurance'/'balanced'/'accelerate'/'chaos')
#   blocked:      'true' if rejected by policy/permission enforcement, else 'false'
#   block_reason: why a blocked attempt was rejected ('not_allowed', 'rate_limited',
#                 'low_battery', 'mode_unsupported', 'no_game', 'handler_error'); empty
#                 string for applied (blocked='false') attempts. Cardinality is bounded
#                 to this fixed reason set, so it is safe as a label.
interventions_total = Counter(
    "game_interventions_total",
    "Total agent intervention attempts (applied or blocked by policy)",
    ["type", "objective", "blocked", "block_reason"],
)

# EventBus stream health metrics (Issue #337)
event_bus_publish_drops_total = Counter(
    "event_bus_publish_drops_total",
    "Total events dropped due to full subscriber queues",
    ["event_type"],
)

event_bus_active_subscribers = Gauge(
    "event_bus_active_subscribers",
    "Number of active EventBus subscribers",
)

event_bus_publish_errors_total = Counter(
    "event_bus_publish_errors_total",
    "Total errors during event publishing",
    ["event_type"],
)
