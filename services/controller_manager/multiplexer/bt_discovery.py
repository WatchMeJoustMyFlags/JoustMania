"""
Centralized Bluetooth Discovery for multi-adapter setups.

Manages all Bluetooth adapters for setups with multiple USB BT dongles.
Each adapter supports ~7 controllers, so multi-adapter setups scale capacity.

Scans via hid.enumerate() with BlueZ cross-ref for adapter affinity.
"""

from __future__ import annotations

import logging
import pathlib

from services.controller_manager import bluetooth, metrics

logger = logging.getLogger(__name__)


class CentralizedBTDiscovery:
    """Manages all Bluetooth adapters for multi-adapter setups.

    Provides:
    - Multi-adapter enumeration and initialization
    - Consolidated address scanning across all adapters
    - Adapter affinity tracking (which adapter each address is on)
    - Periodic refresh for hot-plug support

    Uses hid.enumerate() for device discovery, with BlueZ cross-reference
    for adapter affinity.
    """

    def __init__(self):
        self._adapters: dict[str, str] = {}  # {hci: bt_address}
        self._address_to_adapter: dict[str, str] = {}  # {normalized_addr: hci}

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

    def _resolve_via_pairing_dir(self, serials: set[str]) -> int:
        """Resolve adapter affinity from BlueZ pairing directories.

        BlueZ stores paired devices under /var/lib/bluetooth/<adapter-addr>/<device-addr>/.
        Builds a reverse map from adapter BT address to hci name, then checks
        which adapter directory contains each device serial.

        Returns the number of newly resolved devices.
        """
        bt_dir = pathlib.Path("/var/lib/bluetooth")
        if not bt_dir.exists():
            return 0

        # Reverse map: adapter BT address (uppercase) -> hci name
        addr_to_hci = {addr.upper().replace(":", ""): hci for hci, addr in self._adapters.items()}

        resolved_count = 0
        for normalized in serials:
            if normalized in self._address_to_adapter:
                continue
            # Format as colon-separated MAC for directory lookup
            mac = ":".join(normalized[i : i + 2] for i in range(0, 12, 2))
            for adapter_dir in bt_dir.iterdir():
                if not adapter_dir.is_dir() or adapter_dir.name in ("cache", "settings"):
                    continue
                if (adapter_dir / mac).exists():
                    adapter_key = self._normalize_address(adapter_dir.name)
                    hci = addr_to_hci.get(adapter_key)
                    if hci:
                        self._address_to_adapter[normalized] = hci
                        resolved_count += 1
                        break
        return resolved_count

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

        Enumerates HID devices via hidapi, then cross-references with BlueZ
        for adapter affinity.
        """
        return await self._scan_via_hidapi()

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

        # Pairing directory fallback for devices not found in BlueZ D-Bus.
        # BlueZ stores pairings at /var/lib/bluetooth/<adapter>/<device>/.
        # This resolves uhid devices whose sysfs paths lack hci info.
        unresolved = seen - set(self._address_to_adapter.keys())
        if unresolved:
            count = self._resolve_via_pairing_dir(unresolved)
            if count:
                logger.info(f"Pairing directory resolved adapter affinity for {count} device(s)")

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
