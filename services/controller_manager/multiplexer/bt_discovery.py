"""
Centralized Bluetooth Discovery for multi-adapter setups.

Manages all Bluetooth adapters for setups with multiple USB BT dongles.
Each adapter supports ~7 controllers, so multi-adapter setups scale capacity.

The psmove library has a global API (count_connected() sees ALL controllers),
so we can NOT run multiple BluetoothBackend instances. Instead, a single
BluetoothBackend uses CentralizedBTDiscovery to manage all adapters.
"""

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
    """

    def __init__(self):
        self._adapters: dict[str, str] = {}  # {hci: bt_address}
        self._address_to_adapter: dict[str, str] = {}  # {device_mac: hci}

    @property
    def adapters(self) -> dict[str, str]:
        return dict(self._adapters)

    @property
    def adapter_count(self) -> int:
        return len(self._adapters)

    def get_adapter_for_address(self, address: str) -> str | None:
        return self._address_to_adapter.get(address)

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
        """Scan all adapters for attached device addresses. Updates affinity map."""
        all_addresses: list[str] = []
        self._address_to_adapter.clear()

        for hci in self._adapters:
            try:
                addresses = await bluetooth.get_attached_addresses(hci)
                for addr in addresses:
                    if addr not in self._address_to_adapter:
                        self._address_to_adapter[addr] = hci
                        all_addresses.append(addr)
            except Exception:
                logger.exception(f"Failed to scan {hci}")

        return all_addresses

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
