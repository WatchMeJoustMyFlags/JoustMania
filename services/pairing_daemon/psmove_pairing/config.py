"""Configuration and constants for PS Move pairing daemon."""

import logging
import os

logger = logging.getLogger("psmove-pairing")

# Static configuration from environment
METRICS_PORT = int(os.getenv("METRICS_PORT", "8002"))
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

# Default intervals (used as fallback when flagd is unavailable)
_DEFAULT_POLL_INTERVAL = 10
_DEFAULT_BT_MONITOR_INTERVAL = 5
# bluetooth_backend fallback when flagd is unavailable: rust-hid is the default (#1371)
_DEFAULT_BLUETOOTH_BACKEND = "rust"
# pairing.bluez_restart_mode (UC-001 BR-005, #1446): unknown values and an
# unreachable flagd mean "never", the mode that cannot disconnect controllers
BLUEZ_RESTART_MODES = ("never", "always", "when_idle")
_DEFAULT_BLUEZ_RESTART_MODE = "never"
# pairing.restart_quiet_period (UC-001 BR-005, #2091): seconds without a PS Move on USB
# before a when_idle restart; 0 = restart at the end of the pairing poll
_DEFAULT_RESTART_QUIET_PERIOD = 20

# Feature flag clients (initialized lazily via init_flag_domain)
# system: pairing.* intervals; controller: bluetooth_backend routing
_system_client = None
_controller_client = None
# observability: the `debug` flag (#1788)
_observability_client = None


def init_performance_flags() -> None:
    """Initialize the flag domains for pairing daemon configuration.

    Pairing intervals live in the ``system`` domain and adapter routing
    lives in the ``controller`` domain. Should be called once at daemon
    startup. If flagd is unreachable, flag evaluation silently falls back
    to defaults.
    """
    global _system_client, _controller_client, _observability_client
    try:
        from lib.feature_flags import get_flag_client, init_flag_domain

        init_flag_domain("system")
        _system_client = get_flag_client("system")
        init_flag_domain("controller")
        _controller_client = get_flag_client("controller")
        init_flag_domain("observability")
        _observability_client = get_flag_client("observability")
        logger.info("Flag domains (system, controller, observability) initialized for pairing daemon")
    except Exception as e:
        _system_client = None
        _controller_client = None
        _observability_client = None
        logger.warning(f"Could not initialize feature flags, using defaults: {e}")


def is_debug() -> bool:
    """Whether to attach tracebacks to logged errors (#1788, flags.md §15).

    Read at use time from the `debug` flag; off when it cannot be resolved (no client,
    flagd unreachable, flag missing). The former DEBUG env var is gone (#1907).
    """
    if _observability_client is None:
        return False
    try:
        from openfeature.evaluation_context import EvaluationContext

        return _observability_client.get_boolean_value("debug", False, EvaluationContext())
    except Exception:
        return False


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
    key. Returns the raw flag variant value ("python", "rust" or "unstable");
    usb_pairing maps it to a pairing backend (UC-001 BR-003).

    Falls back to "rust", the flag's default, when flagd is unavailable (#1371).
    """
    if _controller_client is None:
        return _DEFAULT_BLUETOOTH_BACKEND
    try:
        from openfeature.evaluation_context import EvaluationContext

        return _controller_client.get_string_value("bluetooth_backend", _DEFAULT_BLUETOOTH_BACKEND, EvaluationContext())
    except Exception:
        return _DEFAULT_BLUETOOTH_BACKEND


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


def get_bluez_restart_mode() -> str:
    """Whether pairing restarts bluetoothd: "never", "always" or "when_idle" (UC-001 BR-005).

    Read at use time. Fails closed to "never" when flagd is unavailable or the
    value is not one of BLUEZ_RESTART_MODES.
    """
    if _system_client is None:
        return _DEFAULT_BLUEZ_RESTART_MODE
    try:
        from openfeature.evaluation_context import EvaluationContext

        mode = _system_client.get_string_value(
            "pairing.bluez_restart_mode", _DEFAULT_BLUEZ_RESTART_MODE, EvaluationContext()
        )
    except Exception:
        return _DEFAULT_BLUEZ_RESTART_MODE
    if mode not in BLUEZ_RESTART_MODES:
        logger.warning(f"Unknown pairing.bluez_restart_mode {mode!r}, using {_DEFAULT_BLUEZ_RESTART_MODE}")
        return _DEFAULT_BLUEZ_RESTART_MODE
    return mode


def get_restart_quiet_period() -> float:
    """Seconds no PS Move must have been on USB before a when_idle bluetoothd restart (UC-001 BR-005, #2091).

    Read at use time. 0 restarts at the end of the pairing poll (no batching).
    A negative value or an unreachable flagd mean the default (20 s).
    """
    if _system_client is None:
        return _DEFAULT_RESTART_QUIET_PERIOD
    try:
        from openfeature.evaluation_context import EvaluationContext

        seconds = _system_client.get_integer_value(
            "pairing.restart_quiet_period", _DEFAULT_RESTART_QUIET_PERIOD, EvaluationContext()
        )
    except Exception:
        return _DEFAULT_RESTART_QUIET_PERIOD
    if seconds < 0:
        logger.warning(f"Negative pairing.restart_quiet_period {seconds}, using {_DEFAULT_RESTART_QUIET_PERIOD}")
        return _DEFAULT_RESTART_QUIET_PERIOD
    return seconds


# PS Move Bluetooth MAC prefix (Sony)
PSMOVE_BT_PREFIX = "00:06:F7"
