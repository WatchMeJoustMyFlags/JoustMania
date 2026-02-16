"""
Centralized Bluetooth Discovery for multi-adapter setups.

Manages all Bluetooth adapters for setups with multiple USB BT dongles.
Each adapter supports ~7 controllers, so multi-adapter setups scale capacity.

Supports two discovery modes:
- "bluez": Scans via BlueZ D-Bus per adapter (for psmoveapi/BluetoothBackend)
- "hidapi": Scans via hid.enumerate() with BlueZ cross-ref for adapter affinity
"""

from __future__ import annotations

import logging

from services.controller_manager import bluetooth, metrics

logger = logging.getLogger(__name__)


class CentralizedBTDiscovery:
    """Manages all Bluetooth adapters for multi-adapter setups.

    Provides:
    - Multi-adapter enumeration and initialization
    - Consolidated address scanning across all adapters
    - Adapter affinity tracking (which adapter each address is on)
    - Periodic refresh for hot-plug support

    Discovery modes:
    - "bluez": Uses BlueZ get_attached_addresses() per adapter.
      Best for psmoveapi (BluetoothBackend) which uses BlueZ for pairing.
    - "hidapi": Uses hid.enumerate() for device discovery, with BlueZ
      cross-reference for adapter affinity. Best for HidapiBackend which
      talks to controllers via HID (hidraw) instead of BlueZ.
    """

    def __init__(self, discovery_mode: str = "bluez"):
        self._discovery_mode = discovery_mode
        self._adapters: dict[str, str] = {}  # {hci: bt_address}
        self._address_to_adapter: dict[str, str] = {}  # {normalized_addr: hci}

    @property
    def discovery_mode(self) -> str:
        return self._discovery_mode

    @property
    def adapters(self) -> dict[str, str]:
        return dict(self._adapters)

    @property
    def adapter_count(self) -> int:
        return len(self._adapters)

    @staticmethod
    def _normalize_address(address: str) -> str:
        """Normalize BT address to uppercase hex without colons."""
        return address.upper().replace(":", "")

    def get_adapter_for_address(self, address: str) -> str | None:
        """Look up which adapter owns a device address.

        Accepts addresses in any format (with/without colons).
        """
        return self._address_to_adapter.get(self._normalize_address(address))

    async def initialize(self) -> dict[str, str]:
        """Enumerate all BT adapters and enable them. Returns adapter dict."""
        self._adapters = await bluetooth.get_hci_dict()
        if not self._adapters:
            logger.warning("No Bluetooth adapters found")
            return {}

        logger.info(f"Found {len(self._adapters)} adapter(s): {self._adapters}")
        metrics.bluetooth_adapter_count.set(len(self._adapters))

        for hci in self._adapters:
            try:
                await bluetooth.enable_adapter(hci)
                logger.info(f"Enabled adapter {hci}")
            except Exception:
                logger.exception(f"Failed to enable {hci}")

        return self._adapters

    async def get_all_attached_addresses(self) -> list[str]:
        """Scan for attached device addresses. Updates affinity map.

        Uses the configured discovery_mode:
        - "bluez": scans each adapter via BlueZ D-Bus
        - "hidapi": enumerates HID devices, cross-refs with BlueZ for affinity
        """
        if self._discovery_mode == "hidapi":
            return await self._scan_via_hidapi()
        return await self._scan_via_bluez()

    async def _scan_via_bluez(self) -> list[str]:
        """Scan via BlueZ D-Bus per adapter."""
        all_addresses: list[str] = []
        self._address_to_adapter.clear()

        for hci in self._adapters:
            try:
                addresses = await bluetooth.get_attached_addresses(hci)
                for addr in addresses:
                    key = self._normalize_address(addr)
                    if key not in self._address_to_adapter:
                        self._address_to_adapter[key] = hci
                        all_addresses.append(addr)
            except Exception:
                logger.exception(f"Failed to scan {hci}")

        return all_addresses

    async def _scan_via_hidapi(self) -> list[str]:
        """Scan via hid.enumerate() with BlueZ cross-reference for adapter affinity."""
        import hidraw as hid  # hidraw backend sees Bluetooth HID devices

        from lib.psmove_hid import ALL_PRODUCT_IDS, VENDOR_ID

        devices = [d for pid in ALL_PRODUCT_IDS for d in hid.enumerate(VENDOR_ID, pid)]
        hid_serials: list[str] = []
        seen: set[str] = set()
        for dev_info in devices:
            serial = dev_info.get("serial_number", "")
            if not serial:
                continue
            normalized = self._normalize_address(serial)
            if normalized not in seen:
                seen.add(normalized)
                hid_serials.append(normalized)

        # Build adapter affinity via BlueZ cross-reference
        self._address_to_adapter.clear()
        for hci in self._adapters:
            try:
                bt_addresses = await bluetooth.get_attached_addresses(hci)
                for addr in bt_addresses:
                    key = self._normalize_address(addr)
                    if key in seen and key not in self._address_to_adapter:
                        self._address_to_adapter[key] = hci
            except Exception:
                logger.exception(f"Failed to scan {hci} for adapter affinity")

        return hid_serials

    async def refresh_adapters(self) -> list[str]:
        """Re-enumerate adapters, enable new ones. Returns list of new adapter names."""
        current = await bluetooth.get_hci_dict()
        new_adapters = set(current) - set(self._adapters)

        for hci in new_adapters:
            try:
                await bluetooth.enable_adapter(hci)
                logger.info(f"Hot-plug: enabled new adapter {hci}")
            except Exception:
                logger.exception(f"Hot-plug: failed to enable {hci}")

        self._adapters = current
        metrics.bluetooth_adapter_count.set(len(self._adapters))
        return list(new_adapters)
