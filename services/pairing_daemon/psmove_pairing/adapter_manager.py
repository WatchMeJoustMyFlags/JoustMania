"""Bluetooth adapter management — uses dbus-fast backend."""

import logging
from dataclasses import dataclass

from lib.bluez_dbus import restart_systemd_unit as _restart_impl
from psmove_pairing.adapter_manager_modern import AdapterManager as ModernManager

logger = logging.getLogger("psmove-pairing")


@dataclass
class AdapterInfo:
    """Information about a Bluetooth adapter."""

    hci: str
    address: str
    name: str
    device_count: int


class AdapterManager:
    """Manages Bluetooth adapters — delegates to the dbus-fast backend."""

    def __init__(self):
        self._delegate = ModernManager()

    async def refresh_adapters(self) -> dict[str, list[str]]:
        """Refresh the list of adapters and their paired devices."""
        return await self._delegate.refresh_adapters()

    def get_lowest_bt_device(self) -> str:
        """Get the address of the adapter with the fewest paired devices."""
        return self._delegate.get_lowest_bt_device()

    def select_least_loaded_adapter(self) -> AdapterInfo | None:
        """Select the adapter with the fewest paired devices."""
        result = self._delegate.select_least_loaded_adapter()
        if result is None:
            return None
        return AdapterInfo(hci=result.hci, address=result.address, name=result.name, device_count=result.device_count)

    def check_if_not_paired(self, controller_addr: str) -> bool:
        """Check if a controller is not yet paired to any adapter."""
        return self._delegate.check_if_not_paired(controller_addr)


async def restart_systemd_unit(unit: str, mode: str = "replace") -> None:
    """Restart a systemd unit via dbus-fast."""
    await _restart_impl(unit, mode)
