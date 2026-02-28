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

    @staticmethod
    def _resolve_adapter_from_sysfs(hidraw_path: str | bytes) -> str | None:
        """Resolve BT adapter from hidraw device path via sysfs.

        For Bluetooth HID devices, the resolved sysfs path includes the
        adapter name: .../bluetooth/hci0/hci0:XX/.../hidraw/hidrawN
        """
        if isinstance(hidraw_path, bytes):
            hidraw_path = hidraw_path.decode("utf-8", errors="replace")
        hidraw_name = pathlib.Path(hidraw_path).name  # e.g. "hidraw0"
        sysfs = pathlib.Path(f"/sys/class/hidraw/{hidraw_name}")
        if not sysfs.exists():
            return None
        resolved = str(sysfs.resolve())
        for part in resolved.split("/"):
            if part.startswith("hci") and ":" not in part:
                return part
        return None

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

        # Sysfs fallback for devices not found in BlueZ (common with hidapi)
        unresolved = seen - set(self._address_to_adapter.keys())
        if unresolved:
            for dev_info in devices:
                serial = dev_info.get("serial_number", "")
                if not serial:
                    continue
                normalized = self._normalize_address(serial)
                if normalized in unresolved and normalized not in self._address_to_adapter:
                    path = dev_info.get("path", b"")
                    if path:
                        hci = self._resolve_adapter_from_sysfs(path)
                        if hci and hci in self._adapters:
                            self._address_to_adapter[normalized] = hci

            resolved_by_sysfs = len(seen) - len(unresolved - set(self._address_to_adapter.keys()))
            if resolved_by_sysfs > len(seen) - len(unresolved):
                logger.info(
                    f"Sysfs resolved adapter affinity for {resolved_by_sysfs - (len(seen) - len(unresolved))} device(s)"
                )

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
