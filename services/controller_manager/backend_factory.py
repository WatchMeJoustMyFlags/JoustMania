"""
Backend Factory for Controller Manager

Detects platform and creates appropriate backend instance.

Backend selection priority:
  1. OpenFeature "controller_backend" flag (runtime-switchable via flagd)
  2. Platform auto-detection (Linux -> bluetooth)

Flag values are read once at startup.
Runtime changes to these flags require a service restart to take effect.

Creates ControllerIOAdapter instances wrapped in MultiplexerBackend.
  - Mock adapter is always auto-injected (starts with 0 controllers).
  - Controllers are added dynamically via AddController / AddControllers RPCs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from services.controller_manager.backend import ControllerBackend

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.adapter import ControllerIOAdapter
    from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

logger = logging.getLogger(__name__)


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


def _create_adapter_by_name(name: str) -> ControllerIOAdapter:
    """Create a ControllerIOAdapter instance by name.

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


def create_backend() -> ControllerBackend:
    """
    Create appropriate backend based on flags or platform.

    Selection priority:
        1. OpenFeature "controller_backend" flag from performance domain
        2. Platform auto-detection (Linux -> bluetooth)

    Creates ControllerIOAdapter instances wrapped in MultiplexerBackend.
    Comma-separated flag values (e.g. "mock,bluetooth") create multiple
    adapters. A mock adapter is always auto-injected (with 0 controllers)
    for dynamic RPC management.

    Returns:
        ControllerBackend instance (MultiplexerBackend)

    Raises:
        RuntimeError: If no suitable backend available
        ValueError: If backend combination is unsupported (e.g. bluetooth+hidapi)
    """
    from services.controller_manager.multiplexer import MultiplexerBackend
    from services.controller_manager.multiplexer.validation import validate_backend_combination

    backend_name = _resolve_backend_name()
    if not backend_name:
        backend_name = "bluetooth"  # Default on Linux

    logger.info(f"Backend selection: backend_name={backend_name!r}")

    names = [n.strip() for n in backend_name.split(",")]

    # Always include mock adapter for dynamic controller management via RPC.
    # Mock starts with 0 controllers; tests/demos add them via AddController.
    if "mock" not in names:
        names.append("mock")

    if len(names) > 1:
        validate_backend_combination(names)

    bt_discovery = _create_bt_discovery(names)
    adapters = [_create_adapter_by_name(n) for n in names]
    logger.info(f"MultiplexerBackend with adapters: {names}")
    return MultiplexerBackend(adapters=adapters, bt_discovery=bt_discovery)
