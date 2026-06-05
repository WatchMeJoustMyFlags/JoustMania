"""
Backend Factory for Controller Manager

Detects platform and creates appropriate backend instance.

Backend selection priority:
  1. OpenFeature "backend" flag from the controller domain (runtime-switchable via flagd)
  2. Platform auto-detection (Linux -> python)

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

logger = logging.getLogger(__name__)


def _resolve_backend_name() -> str | None:
    """Resolve backend name from OpenFeature flag.

    Returns the backend name string, or None to fall through to platform detection.
    """
    try:
        from lib.feature_flags import get_flag_client

        client = get_flag_client("controller")
        from openfeature.evaluation_context import EvaluationContext

        details = client.get_string_details("backend", "", EvaluationContext())
        backend_flag = details.value
        if backend_flag:
            logger.info(f"Using backend from OpenFeature flag: {backend_flag} (reason={details.reason})")
            return backend_flag.lower()
        logger.warning(
            f"backend flag returned empty string: "
            f"reason={details.reason}, error_code={details.error_code}, "
            f"error_message={details.error_message}"
        )
    except Exception as e:
        logger.warning(f"backend flag evaluation failed: {e}")

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
        case "python":
            from services.controller_manager.multiplexer.python_hid_adapter import PythonHidAdapter

            return PythonHidAdapter()
        case "rust":
            from services.controller_manager.multiplexer.rust_adapter import RustServiceAdapter

            return RustServiceAdapter()
        case _:
            raise RuntimeError(f"Unknown adapter: {name}")


async def initialize_bt_adapters() -> None:
    """Enable Bluetooth adapters (rfkill unblock). Call once at startup."""
    from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

    discovery = CentralizedBTDiscovery()
    await discovery.initialize()


def _is_chaos_enabled() -> bool:
    """Check if chaos testing flag is configured (non-default targeting)."""
    try:
        from openfeature.evaluation_context import EvaluationContext

        from lib.feature_flags import get_flag_client

        client = get_flag_client("controller")
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


def create_backend() -> ControllerBackend:
    """
    Create appropriate backend based on flags or platform.

    Selection priority:
        1. OpenFeature "backend" flag from controller domain
        2. Platform auto-detection (Linux -> python)

    Creates ControllerIOAdapter instances wrapped in MultiplexerBackend.
    Comma-separated flag values (e.g. "mock,python") create multiple
    adapters. A mock adapter is always auto-injected (with 0 controllers)
    for dynamic RPC management.

    Returns:
        ControllerBackend instance (MultiplexerBackend)

    Raises:
        RuntimeError: If no suitable backend available
        ValueError: If backend combination is unsupported
    """
    from services.controller_manager.multiplexer import MultiplexerBackend
    from services.controller_manager.multiplexer.validation import validate_backend_combination

    backend_name = _resolve_backend_name()
    if not backend_name:
        backend_name = "python"  # Default on Linux

    logger.info(f"Backend selection: backend_name={backend_name!r}")

    names = [n.strip() for n in backend_name.split(",")]

    # Always include mock adapter for dynamic controller management via RPC.
    # Mock starts with 0 controllers; tests/demos add them via AddController.
    if "mock" not in names:
        names.append("mock")

    if len(names) > 1:
        validate_backend_combination(names)

    adapters = [_create_adapter_by_name(n) for n in names]
    adapters = _maybe_wrap_chaos(adapters)
    routing_only = _build_routing_only_adapters(adapters)
    logger.info(f"MultiplexerBackend with adapters: {names}")
    return MultiplexerBackend(adapters=adapters, routing_only_adapters=routing_only)


def _build_routing_only_adapters(
    adapters: list[ControllerIOAdapter],
) -> list[ControllerIOAdapter]:
    """Build routing-only adapters (selectable by targeting/rollout, not by default).

    Whenever a python adapter is loaded, an :class:`UnstableAdapter` wrapping the
    *same* python instance is registered so the ``unstable`` backend is routable
    via explicit ``bluetooth_backend`` targeting or the rollout domain. It shares
    the inner python adapter (see UnstableAdapter docstring for why a second
    independent instance is unsafe), so it must NOT be added as an independent
    discoverer and is never chosen as a default/fallback winner.
    """
    from services.controller_manager.multiplexer.unstable_adapter import UnstableAdapter

    routing_only: list[ControllerIOAdapter] = []
    for adapter in adapters:
        if adapter.adapter_type == "python":
            routing_only.append(UnstableAdapter(adapter))
            logger.info("Registered UnstableAdapter (shared inner python) as routing-only target")
    return routing_only
