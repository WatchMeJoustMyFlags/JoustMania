"""
Backend Factory for Controller Manager

Detects platform and creates appropriate backend instance.

Backend selection priority:
  1. OpenFeature "controller_backend" flag (runtime-switchable via flagd)
  2. Platform auto-detection (Linux -> bluetooth, Windows -> windows)

Flag values (controller_backend, mock_controller_count) are read once at startup.
Runtime changes to these flags require a service restart to take effect.
"""

import logging
import platform

from services.controller_manager.backend import ControllerBackend

logger = logging.getLogger(__name__)


def _get_mock_controller_count() -> int:
    """Read mock_controller_count from flagd performance domain."""
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        count = client.get_integer_value("mock_controller_count", 4, EvaluationContext())
        logger.info(f"mock_controller_count from flagd: {count}")
        return count
    except Exception as e:
        logger.warning(f"Failed to read mock_controller_count from flagd, using default: {e}")
        return 4


def _resolve_backend_name() -> str | None:
    """Resolve backend name from OpenFeature flag.

    Returns the backend name string, or None to fall through to platform detection.
    """
    try:
        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        from openfeature.evaluation_context import EvaluationContext

        backend_flag = client.get_string_value("controller_backend", "", EvaluationContext())
        if backend_flag:
            logger.info(f"Using backend from OpenFeature flag: {backend_flag}")
            return backend_flag.lower()
    except Exception as e:
        logger.debug(f"OpenFeature flag evaluation failed, falling back to platform detection: {e}")

    return None


def _create_backend_by_name(name: str) -> ControllerBackend:
    """Create a backend instance by name."""
    match name:
        case "mock":
            from services.controller_manager.mock_backend import MockBackend

            num_controllers = _get_mock_controller_count()
            return MockBackend(num_controllers)
        case "bluetooth":
            from services.controller_manager.bluetooth_backend import BluetoothBackend

            return BluetoothBackend()
        case "hidapi":
            from services.controller_manager.hidapi_backend import HidapiBackend

            return HidapiBackend()
        case "windows":
            from services.controller_manager.windows_backend import WindowsBackend

            return WindowsBackend()
        case _:
            raise RuntimeError(f"Unknown backend: {name}")


def create_backend() -> ControllerBackend:
    """
    Create appropriate backend based on flags or platform.

    Selection priority:
        1. OpenFeature "controller_backend" flag from performance domain
        2. Platform auto-detection (Linux -> bluetooth, Windows -> windows)

    Configuration:
        mock_controller_count: flagd flag (performance domain)

    Returns:
        ControllerBackend instance

    Raises:
        RuntimeError: If no suitable backend available
    """
    backend_name = _resolve_backend_name()

    if backend_name:
        return _create_backend_by_name(backend_name)

    # Priority 3: Platform auto-detection
    system = platform.system()

    if system == "Windows":
        try:
            from services.controller_manager.windows_backend import WindowsBackend

            logger.info("Using Windows backend (psmoveapi)")
            return WindowsBackend()

        except ImportError as e:
            logger.error(f"Windows backend not available: {e}")
            logger.info("Install psmoveapi: pip install psmoveapi")
            logger.info("Or use mock mode: set controller_backend=mock in flagd performance.json")
            raise RuntimeError("Windows backend not available") from e

    elif system == "Linux":
        try:
            from services.controller_manager.bluetooth_backend import BluetoothBackend

            logger.info("Using Linux BlueZ backend")
            return BluetoothBackend()

        except ImportError as e:
            logger.error(f"Bluetooth backend not available: {e}")
            logger.info("Install dependencies: apt-get install python3-dbus, pip install psmove")
            logger.info("Or use mock mode: set controller_backend=mock in flagd performance.json")
            raise RuntimeError("Bluetooth backend not available") from e

    else:
        raise RuntimeError(
            f"Unsupported platform: {system}. "
            "Set controller_backend=mock in flagd performance.json to use mock controllers."
        )
