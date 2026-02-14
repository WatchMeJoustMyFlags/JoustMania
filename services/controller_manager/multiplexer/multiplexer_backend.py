"""
MultiplexerBackend — orchestrator that routes per-controller operations
through ControllerIOAdapter instances with centralized state tracking.

Phase 4: Adapters replace child backends for the multiplexer path.
LED state, rumble, and effect tracking are centralized here instead of
being duplicated across each backend.

Legacy child-based path is preserved for backward compatibility when
MultiplexerBackend is constructed with ControllerBackend children.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from services.controller_manager import metrics
from services.controller_manager.backend import ControllerBackend
from services.controller_manager.multiplexer.adapter import ControllerIOAdapter

if TYPE_CHECKING:
    from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery

logger = logging.getLogger(__name__)


class MultiplexerBackend(ControllerBackend):
    """Orchestrator that routes per-controller calls through adapters.

    When constructed with adapters (Phase 4+):
    - Centralized LED/rumble/effect state tracking
    - Calls adapter.set_output() with combined LED + rumble
    - Adapter assignment tracked in _serial_to_adapter

    When constructed with children (Phase 1-3 legacy):
    - Delegates all operations to child backends
    - No centralized state (children manage their own)
    """

    def __init__(
        self,
        children: list[ControllerBackend] | None = None,
        adapters: list[ControllerIOAdapter] | None = None,
        bt_discovery: CentralizedBTDiscovery | None = None,
    ):
        if not children and not adapters:
            raise ValueError("MultiplexerBackend requires at least one child backend or adapter")

        self._children = children or []
        self._adapters = adapters or []
        self._bt_discovery = bt_discovery

        # Adapter-based routing
        self._serial_to_adapter: dict[str, ControllerIOAdapter] = {}

        # Legacy child-based routing
        self._serial_to_backend: dict[str, ControllerBackend] = {}

        # Centralized state (used only in adapter mode)
        self._led_colors: dict[str, tuple[int, int, int]] = {}
        self._rumble: dict[str, int] = {}
        self._last_sent_color: dict[str, tuple[int, int, int]] = {}
        self._last_led_update: dict[str, float] = {}
        self._effect_active: set[str] = set()
        self._led_lock = threading.Lock()

        if self._adapters:
            adapter_names = [a.adapter_type for a in self._adapters]
            logger.info(f"MultiplexerBackend created with adapters: {adapter_names}")
        if self._children:
            child_names = [c.__class__.__name__ for c in self._children]
            logger.info(f"MultiplexerBackend created with children: {child_names}")

    @property
    def _is_adapter_mode(self) -> bool:
        return len(self._adapters) > 0

    @property
    def adapters(self) -> list[ControllerIOAdapter]:
        """Expose adapters for MockAdapter detection in server.py."""
        return self._adapters

    @property
    def children(self) -> list[ControllerBackend]:
        """Expose children for backward compatibility (server.py MockBackend detection)."""
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

        if self._is_adapter_mode:
            return self._initialize_adapters()

        return await self._initialize_children()

    def _initialize_adapters(self) -> bool:
        """Initialize adapters: discover and open controllers."""
        any_success = False
        for adapter in self._adapters:
            try:
                serials = adapter.discover()
                for serial in serials:
                    adapter.open(serial)
                    self._serial_to_adapter[serial] = adapter
                any_success = True
                logger.info(f"Initialized {adapter.adapter_type} adapter with {len(serials)} controllers")
            except Exception:
                logger.exception(f"Failed to initialize {adapter.adapter_type} adapter")
        return any_success

    async def _initialize_children(self) -> bool:
        """Legacy: initialize child backends."""
        any_success = False
        for child in self._children:
            try:
                if await child.initialize():
                    any_success = True
            except Exception:
                logger.exception(f"Failed to initialize {child.__class__.__name__}")
        return any_success

    async def shutdown(self):
        # Shutdown adapters
        for adapter in self._adapters:
            try:
                adapter.close_all()
            except Exception:
                logger.exception(f"Failed to shutdown {adapter.adapter_type} adapter")

        # Shutdown legacy children
        for child in self._children:
            try:
                await child.shutdown()
            except Exception:
                logger.exception(f"Failed to shutdown {child.__class__.__name__}")

        self._serial_to_adapter.clear()
        self._serial_to_backend.clear()
        self._led_colors.clear()
        self._rumble.clear()
        self._last_sent_color.clear()
        self._last_led_update.clear()
        self._effect_active.clear()

    def get_connected_controllers(self, force_rescan: bool = False) -> list[str]:
        if self._is_adapter_mode:
            return self._get_connected_via_adapters(force_rescan)
        return self._get_connected_via_children(force_rescan)

    def _get_connected_via_adapters(self, force_rescan: bool) -> list[str]:
        """Discover controllers from all adapters, rebuild routing table."""
        seen: dict[str, ControllerIOAdapter] = {}
        for adapter in self._adapters:
            for serial in adapter.discover(force=force_rescan):
                if serial not in seen:
                    seen[serial] = adapter
                    # Ensure handle is open
                    if serial not in self._serial_to_adapter:
                        adapter.open(serial)

        self._serial_to_adapter = seen

        # Clean up state for disconnected controllers
        stale = set(self._led_colors.keys()) - set(seen.keys())
        for serial in stale:
            self._led_colors.pop(serial, None)
            self._rumble.pop(serial, None)
            self._last_sent_color.pop(serial, None)
            self._last_led_update.pop(serial, None)
            self._effect_active.discard(serial)

        # Update metrics
        for serial, adapter in seen.items():
            metrics.controller_backend_info.labels(serial=serial, backend=adapter.adapter_type).set(1)
            if self._bt_discovery:
                hci = self._bt_discovery.get_adapter_for_address(serial)
                if hci:
                    metrics.controller_adapter_info.labels(serial=serial, adapter=hci).set(1)

        return list(seen.keys())

    def _get_connected_via_children(self, force_rescan: bool) -> list[str]:
        """Legacy: get controllers from child backends."""
        seen: dict[str, ControllerBackend] = {}
        for child in self._children:
            for serial in child.get_connected_controllers(force_rescan):
                if serial not in seen:
                    seen[serial] = child

        self._serial_to_backend = seen

        for serial, backend in seen.items():
            metrics.controller_backend_info.labels(serial=serial, backend=backend.__class__.__name__).set(1)
            if self._bt_discovery:
                hci = self._bt_discovery.get_adapter_for_address(serial)
                if hci:
                    metrics.controller_adapter_info.labels(serial=serial, adapter=hci).set(1)

        return list(seen.keys())

    def update_all_leds(self) -> int:
        if self._is_adapter_mode:
            return self._update_all_leds_adapters()

        # Legacy: delegate to children
        total = 0
        for child in self._children:
            total += child.update_all_leds()
        return total

    def _update_all_leds_adapters(self) -> int:
        """Centralized LED refresh with keep-alive and color-change detection."""
        current_time = time.time()
        updated_count = 0

        for serial, stored_color in list(self._led_colors.items()):
            if serial in self._effect_active:
                continue

            adapter = self._serial_to_adapter.get(serial)
            if not adapter:
                continue

            try:
                last_sent = self._last_sent_color.get(serial)
                last_update = self._last_led_update.get(serial, 0)

                color_changed = stored_color != last_sent
                keepalive_needed = current_time - last_update >= 4.0

                if color_changed or keepalive_needed:
                    r, g, b = stored_color
                    rumble = self._rumble.get(serial, 0)
                    with self._led_lock:
                        adapter.set_output(serial, r, g, b, rumble)
                    self._last_sent_color[serial] = stored_color
                    self._last_led_update[serial] = current_time
                    updated_count += 1

                    if color_changed:
                        logger.debug(f"LED color changed for {serial}: {last_sent} -> {stored_color}")

            except Exception as e:
                logger.debug(f"Error updating LED for {serial}: {e}")

        return updated_count

    async def scan_controllers(self) -> list[dict]:
        if self._is_adapter_mode:
            # Adapters discover during get_connected_controllers
            results: list[dict] = []
            for adapter in self._adapters:
                for serial in adapter.discover(force=True):
                    results.append({"address": serial, "serial": serial, "name": f"Controller {serial[-4:]}"})
        else:
            # Legacy: delegate to children
            results = []
            for child in self._children:
                try:
                    results.extend(await child.scan_controllers())
                except Exception:
                    logger.exception(f"scan_controllers failed for {child.__class__.__name__}")

        if self._bt_discovery:
            try:
                await self._bt_discovery.get_all_attached_addresses()
            except Exception:
                logger.debug("Failed to refresh adapter affinity map")

        return results

    # -- Per-controller methods -----------------------------------------------

    async def get_controller_state(self, serial: str) -> dict | None:
        if self._is_adapter_mode:
            adapter = self._serial_to_adapter.get(serial)
            if adapter is None:
                return None
            return adapter.poll(serial)

        owner = self._serial_to_backend.get(serial)
        if owner is None:
            return None
        return await owner.get_controller_state(serial)

    async def set_led_color(self, serial: str, r: int, g: int, b: int) -> bool:
        if self._is_adapter_mode:
            adapter = self._serial_to_adapter.get(serial)
            if adapter is None:
                return False
            self._led_colors[serial] = (r, g, b)
            rumble = self._rumble.get(serial, 0)
            result = adapter.set_output(serial, r, g, b, rumble)
            if result:
                self._last_sent_color[serial] = (r, g, b)
                self._last_led_update[serial] = time.time()
            return result

        owner = self._serial_to_backend.get(serial)
        if owner is None:
            return False
        return await owner.set_led_color(serial, r, g, b)

    async def set_rumble(self, serial: str, intensity: int) -> bool:
        if self._is_adapter_mode:
            adapter = self._serial_to_adapter.get(serial)
            if adapter is None:
                return False
            self._rumble[serial] = intensity
            r, g, b = self._led_colors.get(serial, (0, 0, 0))
            return adapter.set_output(serial, r, g, b, intensity)

        owner = self._serial_to_backend.get(serial)
        if owner is None:
            return False
        return await owner.set_rumble(serial, intensity)

    def set_effect_active(self, serial: str, active: bool):
        if self._is_adapter_mode:
            if active:
                self._effect_active.add(serial)
            else:
                self._effect_active.discard(serial)
            return

        owner = self._serial_to_backend.get(serial)
        if owner is not None:
            owner.set_effect_active(serial, active)

    async def connect_controller(self, address: str) -> bool:
        if self._is_adapter_mode:
            for adapter in self._adapters:
                try:
                    if adapter.open(address):
                        self._serial_to_adapter[address] = adapter
                        return True
                except Exception:
                    logger.exception(f"connect_controller failed for {adapter.adapter_type}")
            return False

        for child in self._children:
            try:
                if await child.connect_controller(address):
                    return True
            except Exception:
                logger.exception(f"connect_controller failed for {child.__class__.__name__}")
        return False

    async def disconnect_controller(self, serial: str) -> bool:
        if self._is_adapter_mode:
            adapter = self._serial_to_adapter.get(serial)
            if adapter is None:
                return False
            adapter.close(serial)
            self._serial_to_adapter.pop(serial, None)
            self._led_colors.pop(serial, None)
            self._rumble.pop(serial, None)
            self._last_sent_color.pop(serial, None)
            self._last_led_update.pop(serial, None)
            self._effect_active.discard(serial)
            return True

        owner = self._serial_to_backend.get(serial)
        if owner is None:
            return False
        return await owner.disconnect_controller(serial)
