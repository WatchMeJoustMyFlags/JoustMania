"""BlueZ Bluetooth helpers for Controller Manager.

Uses dbus-fast via the shared lib.bluez_dbus module.
"""

import logging
import subprocess

from dbus_fast import Variant

from lib.bluez_dbus import (
    ORG_BLUEZ_PATH,
    DBusError,
    get_adapter_property,
    get_managed_objects,
    get_system_bus,
    set_adapter_property,
)

logger = logging.getLogger(__name__)


async def enable_adapter(hci: str) -> bool:
    """Power on the Bluetooth adapter, unblocking rfkill if needed."""
    bus = await get_system_bus()
    adapter_path = f"{ORG_BLUEZ_PATH}/{hci}"
    powered = await get_adapter_property(bus, adapter_path, "Powered")
    if powered:
        return False
    try:
        logger.debug("Enabling adapter")
        await set_adapter_property(bus, adapter_path, "Powered", Variant("b", True))
        return True
    except DBusError as e:
        if "rfkill" in str(e):
            rfkill_unblock(hci)
            return await enable_adapter(hci)
        raise


async def get_attached_addresses(hci: str) -> list[str]:
    """Get MAC addresses of devices known to *hci* via a single GetManagedObjects call."""
    bus = await get_system_bus()
    adapter_path = f"{ORG_BLUEZ_PATH}/{hci}"
    objects = await get_managed_objects(bus)

    addresses: list[str] = []
    for path, interfaces in objects.items():
        # Device paths look like /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF
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


async def get_hci_dict() -> dict[str, str]:
    """Get dictionary mapping hci name to Bluetooth address."""
    bus = await get_system_bus()
    objects = await get_managed_objects(bus)
    hci_dict: dict[str, str] = {}
    for path, interfaces in objects.items():
        if not path.startswith(ORG_BLUEZ_PATH + "/"):
            continue
        adapter_props = interfaces.get("org.bluez.Adapter1")
        if adapter_props is None:
            continue
        hci = path.rsplit("/", 1)[-1]  # e.g. "hci0"
        addr = adapter_props.get("Address")
        if addr is not None:
            val = addr.value if isinstance(addr, Variant) else addr
            hci_dict[hci] = str(val)
    return hci_dict


def rfkill_unblock(hci: str) -> None:
    """Unblock a Bluetooth adapter via rfkill."""
    result = subprocess.run(["rfkill", "list"], capture_output=True, text=True, timeout=5.0)
    hci_id = None
    for line in result.stdout.splitlines():
        if hci in line:
            candidate = line.split(":")[0].strip()
            if candidate.isdigit():
                hci_id = candidate
                break

    if hci_id is None:
        logger.warning(f"Could not find rfkill ID for {hci}")
        return

    subprocess.run(["rfkill", "unblock", hci_id], check=True, timeout=5.0)
