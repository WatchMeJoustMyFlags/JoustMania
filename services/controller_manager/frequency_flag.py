"""
Direct flagd integration for dynamic streaming frequency.

Subscribes to the 'performance' flagd domain and updates streaming
frequency when the 'update_frequency_hz' flag changes. This replaces
the need to relay frequency changes through the Game Coordinator.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# Bounds for frequency clamping
MIN_FREQUENCY_HZ = 1
MAX_FREQUENCY_HZ = 100

# Module-level state: the flag-overridden frequency (None = no override)
_flag_frequency_hz: int | None = None
_lock = threading.Lock()
_initialized = False

# Client reference for flag evaluation
_flag_client = None


def _clamp_frequency(hz: int) -> int:
    """Clamp frequency to valid bounds [1, 100] Hz."""
    return max(MIN_FREQUENCY_HZ, min(MAX_FREQUENCY_HZ, hz))


def get_flag_frequency() -> int | None:
    """Get the current flag-overridden frequency, or None if no override."""
    with _lock:
        return _flag_frequency_hz


def init_frequency_flag() -> None:
    """
    Initialize flagd subscription for the update_frequency_hz flag.

    Registers a PROVIDER_CONFIGURATION_CHANGED handler that updates the
    module-level frequency override when the flag value changes.
    """
    global _initialized, _flag_client
    if _initialized:
        return

    try:
        from openfeature import api
        from openfeature.provider import ProviderEvent

        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain("performance")
        client = get_flag_client("performance")

        # Register event handler for flag changes
        api.add_handler(ProviderEvent.PROVIDER_CONFIGURATION_CHANGED, _on_flags_changed)

        _flag_client = client
        _initialized = True

        # Do initial read
        _refresh_frequency()

        logger.info("Frequency flag subscription initialized")

    except ImportError:
        logger.warning("OpenFeature not available, frequency flag disabled")
    except Exception as e:
        logger.error(f"Failed to initialize frequency flag: {e}")


def _on_flags_changed(_event_details):
    """Event handler called when flagd configuration changes."""
    _refresh_frequency()


def _refresh_frequency():
    """Read current flag value and update the override if changed."""
    global _flag_frequency_hz

    if _flag_client is None:
        return

    try:
        from openfeature.evaluation_context import EvaluationContext

        new_hz = _flag_client.get_integer_value("update_frequency_hz", 0, EvaluationContext())

        # 0 means flag not set or default not resolved - no override
        if new_hz <= 0:
            return

        clamped = _clamp_frequency(new_hz)

        with _lock:
            old = _flag_frequency_hz
            if old != clamped:
                _flag_frequency_hz = clamped
                logger.info(f"Streaming frequency flag updated: {old} -> {clamped} Hz")

                # Update metrics
                try:
                    from services.controller_manager import metrics

                    metrics.stream_frequency_changes_total.inc()
                    metrics.stream_current_frequency_hz.set(clamped)
                except ImportError:
                    pass

    except Exception as e:
        logger.warning(f"Failed to read frequency flag: {e}")
