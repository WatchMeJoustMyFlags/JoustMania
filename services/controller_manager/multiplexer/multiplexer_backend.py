"""
MultiplexerBackend — composite wrapper that routes per-controller operations
to the child backend that owns each serial.

Phase 1: wraps a single child behind the multiplexer_backend_enabled flag.
Phase 2/3: multiple children with per-controller routing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from services.controller_manager import metrics
from services.controller_manager.backend import ControllerBackend

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

logger = logging.getLogger(__name__)


class MultiplexerBackend(ControllerBackend):
    """Routes per-controller calls to the owning child backend.

    Fleet-level methods (initialize, shutdown, get_connected_controllers,
    update_all_leds, scan_controllers) fan out to all children and merge
    results. Per-controller methods (get_controller_state, set_led_color,
    set_rumble, set_effect_active, connect_controller, disconnect_controller)
    look up the owning backend via _serial_to_backend.
    """

    def __init__(
        self,
        children: list[ControllerBackend],
        bt_discovery: CentralizedBTDiscovery | None = None,
    ):
        if not children:
            raise ValueError("MultiplexerBackend requires at least one child backend")
        self._children = children
        self._bt_discovery = bt_discovery
        self._serial_to_backend: dict[str, ControllerBackend] = {}
        child_names = [c.__class__.__name__ for c in children]
        logger.info(f"MultiplexerBackend created with children: {child_names}")

    @property
    def children(self) -> list[ControllerBackend]:
        """Expose children for MockBackend detection in server.py."""
        return self._children

    @property
    def bt_discovery(self) -> CentralizedBTDiscovery | None:
        """Expose bt_discovery for tests and adapter affinity metrics."""
        return self._bt_discovery

    # -- Fleet-level methods --------------------------------------------------

    async def initialize(self) -> bool:
        # Initialize bt_discovery first (adapter enumeration)
        if self._bt_discovery:
            try:
                await self._bt_discovery.initialize()
            except Exception:
                logger.exception("Failed to initialize CentralizedBTDiscovery")

        any_success = False
        for child in self._children:
            try:
                if await child.initialize():
                    any_success = True
            except Exception:
                logger.exception(f"Failed to initialize {child.__class__.__name__}")
        return any_success

    async def shutdown(self):
        for child in self._children:
            try:
                await child.shutdown()
            except Exception:
                logger.exception(f"Failed to shutdown {child.__class__.__name__}")
        self._serial_to_backend.clear()

    def get_connected_controllers(self, force_rescan: bool = False) -> list[str]:
        seen: dict[str, ControllerBackend] = {}
        for child in self._children:
            for serial in child.get_connected_controllers(force_rescan):
                if serial not in seen:
                    seen[serial] = child

        # Rebuild routing table and clean stale entries
        self._serial_to_backend = seen

        # Update backend info metric for each controller
        for serial, backend in seen.items():
            metrics.controller_backend_info.labels(serial=serial, backend=backend.__class__.__name__).set(1)
            # Adapter affinity from CentralizedBTDiscovery
            if self._bt_discovery:
                adapter = self._bt_discovery.get_adapter_for_address(serial)
                if adapter:
                    metrics.controller_adapter_info.labels(serial=serial, adapter=adapter).set(1)

        # Clear metric for serials that disappeared
        # (metric cleanup is best-effort; stale labels age out in the TSDB)

        return list(seen.keys())

    def update_all_leds(self) -> int:
        total = 0
        for child in self._children:
            total += child.update_all_leds()
        return total

    async def scan_controllers(self) -> list[dict]:
        results: list[dict] = []
        for child in self._children:
            try:
                results.extend(await child.scan_controllers())
            except Exception:
                logger.exception(f"scan_controllers failed for {child.__class__.__name__}")

        # Refresh adapter affinity map for metrics
        if self._bt_discovery:
            try:
                await self._bt_discovery.get_all_attached_addresses()
            except Exception:
                logger.debug("Failed to refresh adapter affinity map")

        return results

    # -- Per-controller methods (routed via _serial_to_backend) ---------------

    def _get_owner(self, serial: str) -> ControllerBackend | None:
        return self._serial_to_backend.get(serial)

    async def get_controller_state(self, serial: str) -> dict | None:
        owner = self._get_owner(serial)
        if owner is None:
            return None
        return await owner.get_controller_state(serial)

    async def set_led_color(self, serial: str, r: int, g: int, b: int) -> bool:
        owner = self._get_owner(serial)
        if owner is None:
            return False
        return await owner.set_led_color(serial, r, g, b)

    async def set_rumble(self, serial: str, intensity: int) -> bool:
        owner = self._get_owner(serial)
        if owner is None:
            return False
        return await owner.set_rumble(serial, intensity)

    def set_effect_active(self, serial: str, active: bool):
        owner = self._get_owner(serial)
        if owner is not None:
            owner.set_effect_active(serial, active)

    async def connect_controller(self, address: str) -> bool:
        # Try each child until one succeeds
        for child in self._children:
            try:
                if await child.connect_controller(address):
                    return True
            except Exception:
                logger.exception(f"connect_controller failed for {child.__class__.__name__}")
        return False

    async def disconnect_controller(self, serial: str) -> bool:
        owner = self._get_owner(serial)
        if owner is None:
            return False
        return await owner.disconnect_controller(serial)
