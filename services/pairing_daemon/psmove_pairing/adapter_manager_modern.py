"""Bluetooth adapter management for load-balanced pairing.

Uses dbus-fast via the shared lib.bluez_dbus module.
"""

import logging
from dataclasses import dataclass

from dbus_fast import Variant

from lib.bluez_dbus import (
    ORG_BLUEZ_PATH,
    DBusError,
    get_managed_objects,
    get_system_bus,
)

logger = logging.getLogger("psmove-pairing")


@dataclass
class AdapterInfo:
    """Information about a Bluetooth adapter."""

    hci: str  # e.g., "hci0"
    address: str  # e.g., "AA:BB:CC:DD:EE:FF"
    name: str
    device_count: int


async def get_hci_dict() -> dict[str, str]:
    """Get dictionary mapping hci name to Bluetooth address.

    Returns:
        Dict like {"hci0": "AA:BB:CC:DD:EE:FF", "hci1": "BB:CC:DD:EE:FF:00"}
    """
    bus = await get_system_bus()
    objects = await get_managed_objects(bus)

    hci_dict: dict[str, str] = {}
    for path, interfaces in objects.items():
        adapter_props = interfaces.get("org.bluez.Adapter1")
        if adapter_props is None:
            continue
        # Adapter paths look like /org/bluez/hci0
        if not path.startswith(ORG_BLUEZ_PATH + "/"):
            continue
        hci = path.rsplit("/", 1)[-1]
        addr = adapter_props.get("Address")
        if addr is not None:
            val = addr.value if isinstance(addr, Variant) else addr
            hci_dict[hci] = str(val)

    return hci_dict


async def get_attached_addresses(hci: str) -> list[str]:
    """Get the addresses of devices known by an HCI adapter.

    Args:
        hci: Adapter name like "hci0"

    Returns:
        List of device MAC addresses paired to this adapter
    """
    bus = await get_system_bus()
    adapter_path = f"{ORG_BLUEZ_PATH}/{hci}"
    objects = await get_managed_objects(bus)

    addresses: list[str] = []
    for path, interfaces in objects.items():
        if not path.startswith(adapter_path + "/"):
            continue
        device_props = interfaces.get("org.bluez.Device1")
        if device_props is None:
            continue
        addr = device_props.get("Address")
        if addr is not None:
            val = addr.value if isinstance(addr, Variant) else addr
            addresses.append(str(val))

    return addresses


class AdapterManager:
    """Manages Bluetooth adapters and provides load-balanced selection.

    Uses dbus-fast to query BlueZ directly.
    """

    def __init__(self):
        self._hci_dict: dict[str, str] = {}  # hci -> address
        self._bt_devices: dict[str, list[str]] = {}  # address -> [device_addrs]

    async def refresh_adapters(self) -> dict[str, list[str]]:
        """Refresh the list of adapters and their paired devices.

        Returns:
            Dict mapping adapter address to list of paired device addresses
        """
        try:
            self._hci_dict = await get_hci_dict()
        except DBusError as e:
            logger.debug(f"Error getting adapters: {e}")
            self._hci_dict = {}

        self._bt_devices = {}

        for hci, addr in self._hci_dict.items():
            try:
                devices = await get_attached_addresses(hci)
            except DBusError as e:
                logger.debug(f"Error getting devices for {hci}: {e}")
                devices = []
            self._bt_devices[addr] = devices
            logger.debug(f"Adapter {addr} ({hci}): {len(devices)} devices")

        return self._bt_devices

    def get_lowest_bt_device(self) -> str:
        """Get the address of the adapter with the fewest paired devices.

        This matches the original JoustMania's get_lowest_bt_device() method.
        Call refresh_adapters() first to ensure data is current.

        Returns:
            Bluetooth address of the least-loaded adapter, or empty string if none
        """
        if not self._bt_devices:
            logger.warning("No Bluetooth adapters found")
            return ""

        # Find minimum device count
        min_count = min(len(devices) for devices in self._bt_devices.values())

        # Return first adapter with that count (deterministic ordering)
        for addr in sorted(self._bt_devices.keys()):
            if len(self._bt_devices[addr]) == min_count:
                logger.info(f"Selected adapter {addr} with {min_count} devices (of {len(self._bt_devices)} adapters)")
                return addr

        return ""

    def select_least_loaded_adapter(self) -> AdapterInfo | None:
        """Select the adapter with the fewest paired devices.

        Call refresh_adapters() first to ensure data is current.

        Returns:
            AdapterInfo for the least-loaded adapter, or None if no adapters
        """
        if not self._bt_devices:
            logger.warning("No Bluetooth adapters found")
            return None

        # Find minimum device count
        min_count = min(len(devices) for devices in self._bt_devices.values())

        # Find adapter with that count
        for addr in sorted(self._bt_devices.keys()):
            if len(self._bt_devices[addr]) == min_count:
                # Find the hci name for this address
                hci = next(
                    (h for h, a in self._hci_dict.items() if a == addr),
                    "unknown",
                )
                adapter = AdapterInfo(
                    hci=hci,
                    address=addr,
                    name=f"adapter-{hci}",
                    device_count=min_count,
                )
                logger.info(f"Selected adapter {addr} ({hci}) with {min_count} devices")
                # Log all adapter loads for visibility
                for a, devs in sorted(self._bt_devices.items()):
                    logger.debug(f"  {a}: {len(devs)} devices")
                return adapter

        return None

    def check_if_not_paired(self, controller_addr: str) -> bool:
        """Check if a controller is not yet paired to any adapter.

        Args:
            controller_addr: Controller MAC address

        Returns:
            True if the controller is NOT paired to any adapter
        """
        controller_upper = controller_addr.upper()
        return all(controller_upper not in [d.upper() for d in devices] for devices in self._bt_devices.values())
