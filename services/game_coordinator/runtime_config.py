"""
Runtime Configuration System for JoustMania (Phase 43)

Event-driven configuration holder for game performance parameters.
Provides default values that can be read by game loop.

Phase 44: OpenFeature integration with event-driven flag updates.
Uses PROVIDER_CONFIGURATION_CHANGED events to reactively update config.

Issue #464: Game timing parameters (countdown_phase_duration_ms,
winner_rainbow_duration_ms) are now read from flagd game_settings domain.
countdown_duration_seconds removed; countdown skip is now controlled by
setting countdown_phase_duration_ms=0 (the "skip" variant).
"""

import logging
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

    # Fixed zone thresholds (in g-force units, ~4096 raw = 1g)
    # These define movement intensity zones for activity classification
    zone_still_max: float = 1.1  # < 1.1g = still
    zone_active_max: float = 1.5  # 1.1-1.5g = active movement
    zone_warning_max: float = 2.0  # 1.5-2.0g = warning zone
    # > 2.0g = danger zone

    # Metrics emission interval (emit Prometheus gauges every N frames)
    metrics_emit_interval_frames: int = 6  # ~100ms at 60Hz (10Hz updates)


@dataclass
class GamePerformanceConfig:
    """Runtime configuration for game performance parameters."""

    # Core performance
    # Phase 72: Increased from 30Hz to 60Hz for better responsiveness
    update_frequency_hz: int = 60  # Game loop frequency

    # Winner rainbow effect duration (milliseconds)
    # Controlled via flagd game_settings domain (Issue #464)
    winner_rainbow_duration_ms: int = 3000

    # Countdown phase duration (milliseconds) - each LED phase (red/yellow/green)
    # This value is shared between game_coordinator (beep timing) and controller_manager (LED timing)
    # Controlled via flagd game_settings domain (Issue #464)
    # Set to 0 (the "skip" variant) to skip countdown entirely
    countdown_phase_duration_ms: int = 750

    # Analytics configuration
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)

    # Poll drop threshold: rolling-window drop rate (drops/sec) at or below this
    # value is considered normal USB/Bluetooth noise and won't open a
    # controller_poll_degraded span. Evaluated over a 2-second window.
    poll_drop_threshold: int = 10


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
        self.flag_client = None  # performance domain
        self.game_settings_client = None  # game_settings domain
        self._setup_feature_flags()

    def _setup_feature_flags(self):
        """Initialize feature flag clients and event listeners."""
        try:
            from openfeature import api

            from lib.feature_flags import get_flag_client, init_flag_domain

            # Performance domain (update_frequency_hz, poll_drop_threshold)
            init_flag_domain("performance")
            self.flag_client = get_flag_client("performance")

            # Game settings domain (countdown_phase_duration_ms, winner_rainbow_duration_ms)
            init_flag_domain("game_settings")
            self.game_settings_client = get_flag_client("game_settings")

            logger.info("Feature flag clients initialized (performance, game_settings)")

            # Register event handler for configuration changes
            api.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, self._on_flags_changed)
            logger.info("Registered PROVIDER_CONFIGURATION_CHANGED event handler")

            # Do initial refresh to load current flag values
            self._refresh_from_flags()

        except ImportError:
            self.flag_client = None
            self.game_settings_client = None
            logger.warning("Could not initialize feature flags, using defaults")
        except Exception as e:
            self.flag_client = None
            self.game_settings_client = None
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

        Reads from two domains:
        - performance: update_frequency_hz, poll_drop_threshold
        - game_settings: countdown_phase_duration_ms, winner_rainbow_duration_ms
        """
        if not self.flag_client:
            return

        try:
            # Import metrics (lazy to avoid circular dependency)
            from services.game_coordinator import metrics

            with self._config_lock:
                # === Performance domain flags ===

                # Update frequency (15/30/60 Hz)
                old_hz = self.config.update_frequency_hz
                new_hz = self.flag_client.get_integer_value(
                    "update_frequency_hz", self.config.update_frequency_hz, EvaluationContext()
                )
                if new_hz != old_hz:
                    logger.info(f"Config updated: update_frequency_hz {old_hz} -> {new_hz} Hz")
                    self.config.update_frequency_hz = new_hz
                    metrics.config_changes_total.labels(parameter="update_frequency_hz").inc()

                # Track flag evaluation
                metrics.flag_evaluations_total.labels(flag_key="update_frequency_hz").inc()

                # Poll drop threshold (per-frame drops below this are normal noise)
                old_threshold = self.config.poll_drop_threshold
                new_threshold = self.flag_client.get_integer_value(
                    "poll_drop_threshold", self.config.poll_drop_threshold, EvaluationContext()
                )
                if new_threshold != old_threshold:
                    logger.info(f"Config updated: poll_drop_threshold {old_threshold} -> {new_threshold}")
                    self.config.poll_drop_threshold = new_threshold
                    metrics.config_changes_total.labels(parameter="poll_drop_threshold").inc()

                metrics.flag_evaluations_total.labels(flag_key="poll_drop_threshold").inc()

                # Update current config gauges
                metrics.current_update_frequency_hz.set(self.config.update_frequency_hz)

                # === Game settings domain flags ===

                if self.game_settings_client:
                    # Countdown phase duration (0 = skip countdown entirely)
                    old_phase = self.config.countdown_phase_duration_ms
                    new_phase = self.game_settings_client.get_integer_value(
                        "countdown_phase_duration_ms", self.config.countdown_phase_duration_ms, EvaluationContext()
                    )
                    if new_phase != old_phase:
                        logger.info(f"Config updated: countdown_phase_duration_ms {old_phase} -> {new_phase} ms")
                        self.config.countdown_phase_duration_ms = new_phase
                        metrics.config_changes_total.labels(parameter="countdown_phase_duration_ms").inc()

                    metrics.flag_evaluations_total.labels(flag_key="countdown_phase_duration_ms").inc()

                    # Winner rainbow duration
                    old_rainbow = self.config.winner_rainbow_duration_ms
                    new_rainbow = self.game_settings_client.get_integer_value(
                        "winner_rainbow_duration_ms", self.config.winner_rainbow_duration_ms, EvaluationContext()
                    )
                    if new_rainbow != old_rainbow:
                        logger.info(f"Config updated: winner_rainbow_duration_ms {old_rainbow} -> {new_rainbow} ms")
                        self.config.winner_rainbow_duration_ms = new_rainbow
                        metrics.config_changes_total.labels(parameter="winner_rainbow_duration_ms").inc()

                    metrics.flag_evaluations_total.labels(flag_key="winner_rainbow_duration_ms").inc()

        except Exception as e:
            # Don't crash on flag evaluation failure, just log and keep defaults
            logger.warning(f"Failed to evaluate flags: {e}")

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
