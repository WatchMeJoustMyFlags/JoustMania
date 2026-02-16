"""
Backend Factory for Controller Manager

Detects platform and creates appropriate backend instance.

Backend selection priority:
  1. OpenFeature "controller_backend" flag (runtime-switchable via flagd)
  2. Platform auto-detection (Linux -> bluetooth)

Flag values are read once at startup.
Runtime changes to these flags require a service restart to take effect.

Multiplexer path (Phase 4+): creates ControllerIOAdapter instances.
  - Mock adapter is always auto-injected (starts with 0 controllers).
  - Controllers are added dynamically via AddController / AddControllers RPCs.
Legacy path (multiplexer disabled): creates ControllerBackend instances.
  - mock_controller_count flag still controls initial controller count.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from services.controller_manager.backend import ControllerBackend

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
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

        details = client.get_string_details("controller_backend", "", EvaluationContext())
        backend_flag = details.value
        if backend_flag:
            logger.info(f"Using backend from OpenFeature flag: {backend_flag} (reason={details.reason})")
            return backend_flag.lower()
        logger.warning(
            f"controller_backend flag returned empty string: "
            f"reason={details.reason}, error_code={details.error_code}, "
            f"error_message={details.error_message}"
        )
    except Exception as e:
        logger.warning(f"controller_backend flag evaluation failed: {e}")

    return None


def _create_backend_by_name(name: str, bt_discovery: CentralizedBTDiscovery | None = None) -> ControllerBackend:
    """Create a legacy backend instance by name (multiplexer disabled path)."""
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


def _create_adapter_by_name(name: str) -> ControllerIOAdapter:
    """Create a ControllerIOAdapter instance by name (multiplexer enabled path).

    Mock adapters always start with 0 controllers — controllers are added
    dynamically via the AddController / AddControllers RPCs.
    """
    match name:
        case "mock":
            from services.controller_manager.multiplexer.mock_adapter import MockAdapter

            return MockAdapter(num_controllers=0)
        case "bluetooth":
            from services.controller_manager.multiplexer.psmove_adapter import PsMoveAdapter

            return PsMoveAdapter()
        case "hidapi":
            from services.controller_manager.multiplexer.hidapi_adapter import HidapiAdapter

            return HidapiAdapter()
        case _:
            raise RuntimeError(f"Unknown adapter: {name}")


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


def _is_chaos_enabled() -> bool:
    """Check if chaos testing flag is configured (non-default targeting)."""
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        client.get_string_value("chaos_fault_type", "none", EvaluationContext())
        return True
    except Exception:
        return False


def _maybe_wrap_chaos(adapters: list[ControllerIOAdapter]) -> list[ControllerIOAdapter]:
    """Wrap non-mock adapters in ChaosAdapter if chaos flag is configured."""
    if not _is_chaos_enabled():
        return adapters

    from services.controller_manager.multiplexer.chaos_adapter import ChaosAdapter

    wrapped = []
    for adapter in adapters:
        if adapter.adapter_type == "mock":
            wrapped.append(adapter)
        else:
            wrapped.append(ChaosAdapter(adapter))
            logger.info(f"Wrapped {adapter.adapter_type} adapter in ChaosAdapter")
    return wrapped


def _is_multiplexer_enabled() -> bool:
    """Check if the multiplexer_backend_enabled flag is on."""
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("performance")
        details = client.get_boolean_details("multiplexer_backend_enabled", False, EvaluationContext())
        logger.info(
            f"multiplexer_backend_enabled={details.value} (reason={details.reason}, error_code={details.error_code})"
        )
        return details.value
    except Exception as e:
        logger.warning(f"multiplexer_backend_enabled flag evaluation failed: {e}")
        return False


def create_backend() -> ControllerBackend:
    """
    Create appropriate backend based on flags or platform.

    Selection priority:
        1. OpenFeature "controller_backend" flag from performance domain
        2. Platform auto-detection (Linux -> bluetooth)

    If multiplexer_backend_enabled flag is on, creates ControllerIOAdapter
    instances wrapped in MultiplexerBackend. Comma-separated flag values
    (e.g. "mock,bluetooth") create multiple adapters. A mock adapter is
    always auto-injected (with 0 controllers) for dynamic RPC management.

    Configuration:
        mock_controller_count: flagd flag (performance domain, legacy path only)

    Returns:
        ControllerBackend instance

    Raises:
        RuntimeError: If no suitable backend available
        ValueError: If backend combination is unsupported (e.g. bluetooth+hidapi)
    """
    backend_name = _resolve_backend_name()
    logger.info(f"Backend selection: backend_name={backend_name!r}")

    if backend_name and _is_multiplexer_enabled():
        from services.controller_manager.multiplexer import MultiplexerBackend
        from services.controller_manager.multiplexer.validation import validate_backend_combination

        names = [n.strip() for n in backend_name.split(",")]

        # Always include mock adapter for dynamic controller management via RPC.
        # Mock starts with 0 controllers; tests/demos add them via AddController.
        if "mock" not in names:
            names.append("mock")

        if len(names) > 1:
            validate_backend_combination(names)
        bt_discovery = _create_bt_discovery(names)
        adapters = [_create_adapter_by_name(n) for n in names]
        adapters = _maybe_wrap_chaos(adapters)
        logger.info(f"MultiplexerBackend with adapters: {names}")
        return MultiplexerBackend(adapters=adapters, bt_discovery=bt_discovery)

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

    return backend
