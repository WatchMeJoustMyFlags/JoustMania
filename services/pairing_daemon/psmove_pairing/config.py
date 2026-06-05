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

# Feature flag clients (initialized lazily via init_flag_domain)
# system: pairing.* intervals; controller: bluetooth_backend routing
_system_client = None
_controller_client = None


def init_performance_flags() -> None:
    """Initialize the flag domains for pairing daemon configuration.

    Pairing intervals live in the ``system`` domain and adapter routing
    lives in the ``controller`` domain. Should be called once at daemon
    startup. If flagd is unreachable, flag evaluation silently falls back
    to defaults.
    """
    global _system_client, _controller_client
    try:
        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain("system")
        _system_client = get_flag_client("system")
        init_flag_domain("controller")
        _controller_client = get_flag_client("controller")
        logger.info("Flag domains (system, controller) initialized for pairing daemon")
    except Exception as e:
        _system_client = None
        _controller_client = None
        logger.warning(f"Could not initialize feature flags, using defaults: {e}")


def get_poll_interval() -> int:
    """Get the current USB poll interval in seconds.

    Re-evaluates from flagd on each call for runtime tunability.
    Falls back to default (10s) when flagd is unavailable.
    """
    if _system_client is None:
        return _DEFAULT_POLL_INTERVAL
    try:
        from openfeature.evaluation_context import EvaluationContext

        return _system_client.get_integer_value("pairing.poll_interval", _DEFAULT_POLL_INTERVAL, EvaluationContext())
    except Exception:
        return _DEFAULT_POLL_INTERVAL


def get_adapter_routing_default() -> str:
    """Get the default adapter routing backend.

    Reads bluetooth_backend from the controller domain with no targeting
    key. Returns "hidapi" or "rust".

    Falls back to "hidapi" when flagd is unavailable.
    """
    if _controller_client is None:
        return "hidapi"
    try:
        from openfeature.evaluation_context import EvaluationContext

        return _controller_client.get_string_value("bluetooth_backend", "hidapi", EvaluationContext())
    except Exception:
        return "hidapi"


def get_bt_monitor_interval() -> int:
    """Get the current Bluetooth monitor interval in seconds.

    Re-evaluates from flagd on each call for runtime tunability.
    Falls back to default (5s) when flagd is unavailable.
    """
    if _system_client is None:
        return _DEFAULT_BT_MONITOR_INTERVAL
    try:
        from openfeature.evaluation_context import EvaluationContext

        return _system_client.get_integer_value(
            "pairing.bt_monitor_interval", _DEFAULT_BT_MONITOR_INTERVAL, EvaluationContext()
        )
    except Exception:
        return _DEFAULT_BT_MONITOR_INTERVAL


# PS Move Bluetooth MAC prefix (Sony)
PSMOVE_BT_PREFIX = "00:06:F7"
