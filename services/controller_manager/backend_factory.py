"""
Backend Factory for Controller Manager

Detects platform and creates appropriate backend instance.

Backend selection priority:
  1. OpenFeature "controller_backend" flag (runtime-switchable via flagd)
  2. Platform auto-detection (Linux -> bluetooth)

Flag values (controller_backend, mock_controller_count) are read once at startup.
Runtime changes to these flags require a service restart to take effect.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from services.controller_manager.backend import ControllerBackend

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

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


def _create_backend_by_name(name: str, bt_discovery: CentralizedBTDiscovery | None = None) -> ControllerBackend:
    """Create a backend instance by name."""
    match name:
        case "mock":
            from services.controller_manager.mock_backend import MockBackend

            num_controllers = _get_mock_controller_count()
            return MockBackend(num_controllers)
        case "bluetooth":
            from services.controller_manager.bluetooth_backend import BluetoothBackend

            return BluetoothBackend(bt_discovery=bt_discovery)
        case "hidapi":
            from services.controller_manager.hidapi_backend import HidapiBackend

            return HidapiBackend()
        case _:
            raise RuntimeError(f"Unknown backend: {name}")


def _create_bt_discovery(names: list[str]) -> CentralizedBTDiscovery | None:
    """Create CentralizedBTDiscovery if any backend needs Bluetooth.

    Returns None if no backend in the list uses Bluetooth.
    Discovery mode is determined by the backend type:
    - "bluez" for BluetoothBackend (psmoveapi + BlueZ scanning)
    - "hidapi" for HidapiBackend (hid.enumerate scanning)
    """
    from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

    if "bluetooth" in names:
        return CentralizedBTDiscovery(discovery_mode="bluez")
    if "hidapi" in names:
        return CentralizedBTDiscovery(discovery_mode="hidapi")
    return None


def _is_multiplexer_enabled() -> bool:
    """Check if the multiplexer_backend_enabled flag is on."""
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        return client.get_boolean_value("multiplexer_backend_enabled", False, EvaluationContext())
    except Exception:
        return False


def create_backend() -> ControllerBackend:
    """
    Create appropriate backend based on flags or platform.

    Selection priority:
        1. OpenFeature "controller_backend" flag from performance domain
        2. Platform auto-detection (Linux -> bluetooth)

    If multiplexer_backend_enabled flag is on, wraps the resolved backend(s)
    in a MultiplexerBackend composite. Comma-separated flag values (e.g.
    "mock,bluetooth") create multiple children.

    Configuration:
        mock_controller_count: flagd flag (performance domain)

    Returns:
        ControllerBackend instance

    Raises:
        RuntimeError: If no suitable backend available
        ValueError: If backend combination is unsupported (e.g. bluetooth+hidapi)
    """
    backend_name = _resolve_backend_name()

    if backend_name and _is_multiplexer_enabled():
        names = [n.strip() for n in backend_name.split(",")]
        if len(names) > 1:
            from services.controller_manager.multiplexer import MultiplexerBackend
            from services.controller_manager.multiplexer.validation import validate_backend_combination

            validate_backend_combination(names)
            bt_discovery = _create_bt_discovery(names)
            children = [_create_backend_by_name(n, bt_discovery=bt_discovery) for n in names]
            logger.info(f"MultiplexerBackend with children: {names}")
            return MultiplexerBackend(children, bt_discovery=bt_discovery)

        bt_discovery = _create_bt_discovery(names)
        backend = _create_backend_by_name(names[0], bt_discovery=bt_discovery)
        logger.info(f"Wrapping {backend.__class__.__name__} in MultiplexerBackend")
        from services.controller_manager.multiplexer import MultiplexerBackend

        return MultiplexerBackend([backend], bt_discovery=bt_discovery)

    if backend_name:
        # Legacy path (multiplexer disabled) — take first name if comma-separated
        name = backend_name.split(",")[0].strip()
        return _create_backend_by_name(name)

    # Priority 2: Default to Bluetooth on Linux (only supported platform)
    try:
        from services.controller_manager.bluetooth_backend import BluetoothBackend

        logger.info("Using Linux BlueZ backend")
        backend = BluetoothBackend()

    except ImportError as e:
        logger.error(f"Bluetooth backend not available: {e}")
        logger.info("Install dependencies: apt-get install python3-dbus, pip install psmove")
        logger.info("Or use mock mode: set controller_backend=mock in flagd performance.json")
        raise RuntimeError("Bluetooth backend not available") from e

    if _is_multiplexer_enabled():
        from services.controller_manager.multiplexer import MultiplexerBackend

        logger.info(f"Wrapping {backend.__class__.__name__} in MultiplexerBackend")
        return MultiplexerBackend([backend])

    return backend
