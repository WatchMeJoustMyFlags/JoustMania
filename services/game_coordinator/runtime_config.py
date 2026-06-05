"""
Runtime Configuration System for JoustMania (Phase 43)

Event-driven configuration holder for game performance parameters.
Provides default values that can be read by game loop.

Phase 44: OpenFeature integration with event-driven flag updates.
Uses PROVIDER_CONFIGURATION_CHANGED events to reactively update config.

Issue #464: Game timing parameters (countdown_phase_duration_ms,
winner_rainbow_duration_ms) are now read from flagd game domain.
countdown_duration_seconds removed; countdown skip is now controlled by
setting countdown_phase_duration_ms=0 (the "skip" variant).
"""

import logging
import math
import threading
from dataclasses import dataclass, field

from openfeature.evaluation_context import EvaluationContext
from openfeature.provider import ProviderEvent

logger = logging.getLogger(__name__)


@dataclass
class AnalyticsConfig:
    """Configuration for controller analytics during gameplay."""

    # Master toggle
    enabled: bool = True

    # Feature toggles
    track_gyro: bool = False  # Track rotation data (increases memory/cpu slightly)
    enable_replay: bool = False  # Store 60Hz samples for replay/testing
    replay_ttl_seconds: int = 3600  # Redis TTL for replay data (1 hour)

    # Zone thresholds (in g-force units, ~4096 raw = 1g). Defaults promoted to
    # the agent domain `perception.zone_*` flags (#766 F4); the values here are
    # the behavior-neutral fallbacks used when flagd is unavailable/malformed.
    # These define movement intensity zones for activity classification.
    zone_still_max: float = 1.1  # < 1.1g = still
    zone_active_max: float = 1.5  # 1.1-1.5g = active movement
    zone_warning_max: float = 2.0  # 1.5-2.0g = warning zone
    # > 2.0g = danger zone

    # Playstyle classification thresholds (percentages of time-in-zone).
    # Promoted to agent-domain `perception.playstyle.*` flags (#766 F4); the
    # defaults below reproduce the previously hardcoded analytics.py literals.
    # Consumed by PlayerAnalytics.get_playstyle (see games/analytics.py).
    playstyle_aggressive_warning_danger_min: float = 30.0  # > this => AGGRESSIVE
    playstyle_calm_still_min: float = 70.0  # still % above this (+ low w/d) => CALM
    playstyle_calm_warning_danger_max: float = 10.0  # w/d % below this for CALM
    playstyle_balanced_still_min: float = 40.0  # still % above this => BALANCED
    playstyle_balanced_warning_danger_max: float = 20.0  # w/d % below this for BALANCED

    # Metrics emission interval (emit Prometheus gauges every N frames)
    metrics_emit_interval_frames: int = 6  # ~100ms at 60Hz (10Hz updates)


@dataclass
class GamePerformanceConfig:
    """Runtime configuration for game performance parameters."""

    # Core performance
    # Phase 72: Increased from 30Hz to 60Hz for better responsiveness
    update_frequency_hz: int = 60  # Game loop frequency

    # Winner rainbow effect duration (milliseconds)
    # Controlled via flagd game domain (Issue #464)
    winner_rainbow_duration_ms: int = 3000

    # Countdown phase duration (milliseconds) - each LED phase (red/yellow/green)
    # This value is shared between game_coordinator (beep timing) and controller_manager (LED timing)
    # Controlled via flagd game domain (Issue #464)
    # Set to 0 (the "skip" variant) to skip countdown entirely
    countdown_phase_duration_ms: int = 750

    # Analytics configuration
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)

    # Poll drop threshold: rolling-window drop rate (drops/sec) at or below this
    # value is considered normal USB/Bluetooth noise and won't open a
    # controller_poll_degraded span. Evaluated over a 2-second window.
    poll_drop_threshold: int = 10

    # EMA filter weight for acceleration smoothing (#766 F4, agent domain flag
    # `perception.ema_weight`). Formula: smoothed = (smoothed * W + raw) / (W + 1).
    # W=4 gives 80% weight to the previous value (the historical default).
    #
    # READ AT GAME INIT ONLY — never re-evaluated mid-game. Changing the EMA
    # weight while a game is running would invalidate the per-player variance
    # baseline that the agent's perception layer depends on (#722 §5: difficulty
    # interventions "invalidate the variance baseline"). The value is therefore
    # frozen into each game at __init__ and not refreshed by flag-change events.
    ema_weight: float = 4.0


class RuntimeConfigManager:
    """
    Manages runtime configuration with event-driven flag updates.

    Phase 43: Simple configuration holder with defaults.
    Phase 44: Event-driven OpenFeature integration.

    Uses PROVIDER_CONFIGURATION_CHANGED events to reactively update configuration,
    eliminating the need for polling and reducing load on flagd.
    """

    def __init__(self):
        self.config = GamePerformanceConfig()
        self._config_lock = threading.RLock()  # Protect config updates

        # Initialize feature flag clients
        self.system_client = None  # system domain
        self.controller_client = None  # controller domain
        self.game_client = None  # game domain
        self.agent_client = None  # agent domain (perception.* — #766 F4)
        self._setup_feature_flags()

    def _setup_feature_flags(self):
        """Initialize feature flag clients and event listeners."""
        try:
            from openfeature import api

            from lib.feature_flags import get_flag_client, init_flag_domain

            # System domain (game_loop.update_frequency_hz)
            init_flag_domain("system")
            self.system_client = get_flag_client("system")

            # Controller domain (poll_drop_threshold)
            init_flag_domain("controller")
            self.controller_client = get_flag_client("controller")

            # Game domain (countdown_phase_duration_ms, winner_rainbow_duration_ms)
            init_flag_domain("game")
            self.game_client = get_flag_client("game")

            # Agent domain (perception.* — #766 F4): zone boundaries + playstyle
            # classification thresholds (change-event refreshed) and ema_weight
            # (read once per game at init; see read_ema_weight).
            init_flag_domain("agent")
            self.agent_client = get_flag_client("agent")

            logger.info("Feature flag clients initialized (system, controller, game, agent)")

            # Register event handler for configuration changes
            api.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, self._on_flags_changed)
            logger.info("Registered PROVIDER_CONFIGURATION_CHANGED event handler")

            # Do initial refresh to load current flag values
            self._refresh_from_flags()

        except ImportError:
            self.system_client = None
            self.controller_client = None
            self.game_client = None
            self.agent_client = None
            logger.warning("Could not initialize feature flags, using defaults")
        except Exception as e:
            self.system_client = None
            self.controller_client = None
            self.game_client = None
            self.agent_client = None
            logger.error(f"Failed to initialize feature flags: {e}")

    def _on_flags_changed(self, event_details):
        """
        Event handler called when feature flags change.

        This is triggered by flagd's gRPC sync stream when flag configurations
        are updated, providing instant updates without polling.
        """
        # Import metrics (lazy to avoid circular dependency)
        try:
            from services.game_coordinator import metrics

            metrics.flag_configuration_changes_total.inc()
        except ImportError:
            pass

        changed_flags = getattr(event_details, "flags_changed", [])
        if changed_flags:
            logger.info(f"🚩 Feature flags changed: {changed_flags}")
        else:
            logger.info("🚩 Feature flags changed (unspecified flags)")

        # Refresh configuration from updated flags
        self._refresh_from_flags()

    def _refresh_from_flags(self):
        """
        Update configuration from feature flags.

        Called during initialization and when PROVIDER_CONFIGURATION_CHANGED
        event fires. Thread-safe and includes metrics tracking.

        Reads from three domains:
        - system: game_loop.update_frequency_hz
        - controller: poll_drop_threshold
        - game: countdown_phase_duration_ms, winner_rainbow_duration_ms
        """
        if not self.system_client:
            return

        try:
            # Import metrics (lazy to avoid circular dependency)
            from services.game_coordinator import metrics

            with self._config_lock:
                # === System domain flags ===

                # Update frequency (15/30/60 Hz)
                old_hz = self.config.update_frequency_hz
                new_hz = self.system_client.get_integer_value(
                    "game_loop.update_frequency_hz", self.config.update_frequency_hz, EvaluationContext()
                )
                if new_hz != old_hz:
                    logger.info(f"Config updated: update_frequency_hz {old_hz} -> {new_hz} Hz")
                    self.config.update_frequency_hz = new_hz
                    metrics.config_changes_total.labels(parameter="update_frequency_hz").inc()

                # Track flag evaluation
                metrics.flag_evaluations_total.labels(flag_key="game_loop.update_frequency_hz").inc()

                # === Controller domain flags ===

                # Poll drop threshold (per-frame drops below this are normal noise)
                if self.controller_client:
                    old_threshold = self.config.poll_drop_threshold
                    new_threshold = self.controller_client.get_integer_value(
                        "poll_drop_threshold", self.config.poll_drop_threshold, EvaluationContext()
                    )
                    if new_threshold != old_threshold:
                        logger.info(f"Config updated: poll_drop_threshold {old_threshold} -> {new_threshold}")
                        self.config.poll_drop_threshold = new_threshold
                        metrics.config_changes_total.labels(parameter="poll_drop_threshold").inc()

                    metrics.flag_evaluations_total.labels(flag_key="poll_drop_threshold").inc()

                # Update current config gauges
                metrics.current_update_frequency_hz.set(self.config.update_frequency_hz)

                # === Game domain flags ===

                if self.game_client:
                    # Countdown phase duration (0 = skip countdown entirely)
                    old_phase = self.config.countdown_phase_duration_ms
                    new_phase = self.game_client.get_integer_value(
                        "countdown_phase_duration_ms", self.config.countdown_phase_duration_ms, EvaluationContext()
                    )
                    if new_phase != old_phase:
                        logger.info(f"Config updated: countdown_phase_duration_ms {old_phase} -> {new_phase} ms")
                        self.config.countdown_phase_duration_ms = new_phase
                        metrics.config_changes_total.labels(parameter="countdown_phase_duration_ms").inc()

                    metrics.flag_evaluations_total.labels(flag_key="countdown_phase_duration_ms").inc()

                    # Winner rainbow duration
                    old_rainbow = self.config.winner_rainbow_duration_ms
                    new_rainbow = self.game_client.get_integer_value(
                        "winner_rainbow_duration_ms", self.config.winner_rainbow_duration_ms, EvaluationContext()
                    )
                    if new_rainbow != old_rainbow:
                        logger.info(f"Config updated: winner_rainbow_duration_ms {old_rainbow} -> {new_rainbow} ms")
                        self.config.winner_rainbow_duration_ms = new_rainbow
                        metrics.config_changes_total.labels(parameter="winner_rainbow_duration_ms").inc()

                    metrics.flag_evaluations_total.labels(flag_key="winner_rainbow_duration_ms").inc()

                # === Agent domain flags (#766 F4: perception.*) ===
                #
                # Zone boundaries and playstyle thresholds follow the natural read
                # point of their consumers — they live on `config.analytics`, read
                # fresh by each game at start — so they are refreshed here on flag
                # change, consistent with this manager's other domains.
                #
                # NOTE: `perception.ema_weight` is deliberately NOT read here. It is
                # read once at game init (see _read_ema_weight_at_init) and frozen
                # for the life of the game (#722 §5 variance-baseline caveat).
                if self.agent_client:
                    self._refresh_perception_flags(metrics)

        except Exception as e:
            # Don't crash on flag evaluation failure, just log and keep defaults
            logger.warning(f"Failed to evaluate flags: {e}")

    def _refresh_perception_flags(self, metrics) -> None:
        """Refresh zone-boundary and playstyle thresholds from the agent domain.

        Each value validates to a positive number; on malformed/invalid values
        the current config value (ultimately the hardcoded default) is kept, so
        the promotion is behavior-neutral. Caller holds ``self._config_lock``.
        """
        analytics = self.config.analytics

        # Zone boundaries (g-force). Must be strictly increasing and positive.
        zone_specs = [
            ("perception.zone_still_max", "zone_still_max"),
            ("perception.zone_active_max", "zone_active_max"),
            ("perception.zone_warning_max", "zone_warning_max"),
        ]
        new_zones = {}
        for flag_key, attr in zone_specs:
            current = getattr(analytics, attr)
            value = self.agent_client.get_float_value(flag_key, current, EvaluationContext())
            metrics.flag_evaluations_total.labels(flag_key=flag_key).inc()
            new_zones[attr] = value if self._valid_positive(value) else current

        # Only apply zone boundaries if they form a strictly increasing ladder;
        # otherwise keep the existing (default) values to avoid corrupting
        # classification.
        if new_zones["zone_still_max"] < new_zones["zone_active_max"] < new_zones["zone_warning_max"]:
            for attr, value in new_zones.items():
                if value != getattr(analytics, attr):
                    logger.info(f"Config updated: analytics.{attr} -> {value}")
                    setattr(analytics, attr, value)
                    metrics.config_changes_total.labels(parameter=attr).inc()
        else:
            logger.warning(f"Ignoring perception zone flags (not strictly increasing): {new_zones}; keeping defaults")

        # Playstyle classification thresholds (percentages, 0-100).
        playstyle_specs = [
            ("perception.playstyle.aggressive_warning_danger_min", "playstyle_aggressive_warning_danger_min"),
            ("perception.playstyle.calm_still_min", "playstyle_calm_still_min"),
            ("perception.playstyle.calm_warning_danger_max", "playstyle_calm_warning_danger_max"),
            ("perception.playstyle.balanced_still_min", "playstyle_balanced_still_min"),
            ("perception.playstyle.balanced_warning_danger_max", "playstyle_balanced_warning_danger_max"),
        ]
        for flag_key, attr in playstyle_specs:
            current = getattr(analytics, attr)
            value = self.agent_client.get_float_value(flag_key, current, EvaluationContext())
            metrics.flag_evaluations_total.labels(flag_key=flag_key).inc()
            if not self._valid_percentage(value):
                value = current
            if value != current:
                logger.info(f"Config updated: analytics.{attr} -> {value}")
                setattr(analytics, attr, value)
                metrics.config_changes_total.labels(parameter=attr).inc()

    @staticmethod
    def _valid_positive(value) -> bool:
        """True if value is a finite, strictly positive number."""
        return isinstance(value, (int, float)) and math.isfinite(value) and value > 0

    @staticmethod
    def _valid_percentage(value) -> bool:
        """True if value is a finite number in [0, 100]."""
        return isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 100

    def read_ema_weight(self) -> float:
        """Read the EMA filter weight from the agent domain (#766 F4).

        Called ONCE per game at game init (BaseGameMode.__init__). The value is
        frozen for the life of the game and never re-evaluated mid-game: changing
        the EMA weight while running would invalidate the per-player movement
        variance baseline the agent's perception layer relies on (#722 §5).

        Returns the validated flag value, or the hardcoded default
        (``config.ema_weight``) on a missing/malformed/non-positive value.
        """
        default = self.config.ema_weight
        if not self.agent_client:
            return default
        try:
            value = self.agent_client.get_float_value("perception.ema_weight", default, EvaluationContext())
        except Exception as e:
            logger.warning(f"Failed to read perception.ema_weight, using default: {e}")
            return default
        return value if self._valid_positive(value) else default

    def get_config(self) -> GamePerformanceConfig:
        """
        Get current configuration.

        Configuration is kept up-to-date automatically via event-driven updates,
        so no polling is needed. Thread-safe access via lock.
        """
        with self._config_lock:
            return self.config

    async def get_update_interval(self) -> float:
        """Get current update interval in seconds (1/Hz)."""
        with self._config_lock:
            return 1.0 / self.config.update_frequency_hz

    def export_config(self) -> dict:
        """Export current configuration as dict (for reports/logs)."""
        with self._config_lock:
            return self.config.__dict__.copy()


# Global singleton instance
_global_config_manager: RuntimeConfigManager | None = None


def get_config_manager() -> RuntimeConfigManager:
    """Get the global runtime configuration manager."""
    global _global_config_manager
    if _global_config_manager is None:
        _global_config_manager = RuntimeConfigManager()
    return _global_config_manager


def get_current_config() -> GamePerformanceConfig:
    """Quick access to current configuration."""
    return get_config_manager().get_config()
