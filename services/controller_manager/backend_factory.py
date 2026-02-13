"""
Backend Factory for Controller Manager

Detects platform and creates appropriate backend instance.

Backend selection priority:
  1. CONTROLLER_BACKEND env var (hard override, e.g. for testing)
  2. OpenFeature "controller_backend" flag (runtime-switchable via flagd)
  3. Platform auto-detection (Linux -> bluetooth, Windows -> windows)

Flag values (mock_controller_count, bluetooth_hci) are read once at startup.
Runtime changes to these flags require a service restart to take effect.
"""

import logging
import os
import platform

from services.controller_manager.backend import ControllerBackend

logger = logging.getLogger(__name__)


def _get_mock_controller_count() -> int:
    """Read mock_controller_count from flagd performance domain with env var fallback."""
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        count = client.get_integer_value("mock_controller_count", 4, EvaluationContext())
        logger.info(f"mock_controller_count from flagd: {count}")
        return count
    except Exception as e:
        logger.warning(f"Failed to read mock_controller_count from flagd, using env/default: {e}")
        return int(os.getenv("MOCK_CONTROLLER_COUNT", "4"))


def _get_bluetooth_hci() -> str:
    """Read bluetooth_hci from flagd performance domain with env var fallback."""
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        hci = client.get_string_value("bluetooth_hci", "hci0", EvaluationContext())
        logger.info(f"bluetooth_hci from flagd: {hci}")
        return hci
    except Exception as e:
        logger.warning(f"Failed to read bluetooth_hci from flagd, using env/default: {e}")
        return os.getenv("BLUETOOTH_HCI", "hci0")


def _resolve_backend_name() -> str | None:
    """Resolve backend name from env var or OpenFeature flag.

    Returns the backend name string, or None to fall through to platform detection.
    """
    # Priority 1: Env var hard override
    forced = os.getenv("CONTROLLER_BACKEND", "").lower()
    if forced:
        logger.info(f"Using forced backend from CONTROLLER_BACKEND env: {forced}")
        return forced

    # Priority 2: OpenFeature flag
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
    if name == "mock":
        from services.controller_manager.mock_backend import MockBackend

        num_controllers = _get_mock_controller_count()
        return MockBackend(num_controllers)

    if name == "bluetooth":
        from services.controller_manager.bluetooth_backend import BluetoothBackend

        return BluetoothBackend()

    if name == "hidapi":
        from services.controller_manager.hidapi_backend import HidapiBackend

        return HidapiBackend()

    if name == "windows":
        from services.controller_manager.windows_backend import WindowsBackend

        return WindowsBackend()

    raise RuntimeError(f"Unknown backend: {name}")


def create_backend() -> ControllerBackend:
    """
    Create appropriate backend based on environment, flags, or platform.

    Selection priority:
        1. CONTROLLER_BACKEND env var (hard override)
        2. OpenFeature "controller_backend" flag from performance domain
        3. Platform auto-detection (Linux -> bluetooth, Windows -> windows)

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
            logger.info("Or use mock mode: set CONTROLLER_BACKEND=mock")
            raise RuntimeError("Windows backend not available") from e

    elif system == "Linux":
        try:
            from services.controller_manager.bluetooth_backend import BluetoothBackend

            logger.info("Using Linux BlueZ backend")
            return BluetoothBackend()

        except ImportError as e:
            logger.error(f"Bluetooth backend not available: {e}")
            logger.info("Install dependencies: apt-get install python3-dbus, pip install psmove")
            logger.info("Or use mock mode: set CONTROLLER_BACKEND=mock")
            raise RuntimeError("Bluetooth backend not available") from e

    else:
        raise RuntimeError(f"Unsupported platform: {system}. Set CONTROLLER_BACKEND=mock to use mock controllers.")
