"""Configuration and constants for PS Move pairing daemon."""

import logging
import os

logger = logging.getLogger("psmove-pairing")

# Static configuration from environment
DEBUG = os.getenv("DEBUG", "0") == "1"
METRICS_PORT = int(os.getenv("METRICS_PORT", "8002"))
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

# Default intervals (used as fallback when flagd is unavailable)
_DEFAULT_POLL_INTERVAL = 10
_DEFAULT_BT_MONITOR_INTERVAL = 5

# Feature flag client (initialized lazily via init_flag_domain)
_flag_client = None


def init_performance_flags() -> None:
    """Initialize the performance flag domain for interval configuration.

    Should be called once at daemon startup. If flagd is unreachable,
    flag evaluation silently falls back to defaults.
    """
    global _flag_client
    try:
        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain("performance")
        _flag_client = get_flag_client("performance")
        logger.info("Performance flag domain initialized for pairing daemon")
    except Exception as e:
        _flag_client = None
        logger.warning(f"Could not initialize feature flags, using defaults: {e}")


def get_poll_interval() -> int:
    """Get the current USB poll interval in seconds.

    Re-evaluates from flagd on each call for runtime tunability.
    Falls back to default (10s) when flagd is unavailable.
    """
    if _flag_client is None:
        return _DEFAULT_POLL_INTERVAL
    try:
        from openfeature.evaluation_context import EvaluationContext

        return _flag_client.get_integer_value("pairing_poll_interval", _DEFAULT_POLL_INTERVAL, EvaluationContext())
    except Exception:
        return _DEFAULT_POLL_INTERVAL


def get_bt_monitor_interval() -> int:
    """Get the current Bluetooth monitor interval in seconds.

    Re-evaluates from flagd on each call for runtime tunability.
    Falls back to default (5s) when flagd is unavailable.
    """
    if _flag_client is None:
        return _DEFAULT_BT_MONITOR_INTERVAL
    try:
        from openfeature.evaluation_context import EvaluationContext

        return _flag_client.get_integer_value("bt_monitor_interval", _DEFAULT_BT_MONITOR_INTERVAL, EvaluationContext())
    except Exception:
        return _DEFAULT_BT_MONITOR_INTERVAL


# PS Move Bluetooth MAC prefix (Sony)
PSMOVE_BT_PREFIX = "00:06:F7"
