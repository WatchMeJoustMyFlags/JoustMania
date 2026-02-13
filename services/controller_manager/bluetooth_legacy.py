"""BlueZ Bluetooth helpers using dbus-python (legacy backend).

Wraps synchronous dbus-python calls in asyncio.to_thread() to provide the same
async API as the modern dbus-fast backend. This allows bluetooth_backend.py
to use either backend transparently.
"""

import asyncio
import logging
import os
import subprocess
from xml.etree import ElementTree

import dbus

logger = logging.getLogger(__name__)

BUS = dbus.SystemBus()
ORG_BLUEZ = "org.bluez"
ORG_BLUEZ_PATH = "/org/bluez"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
BLUEZ_ADAPTER1_INTERFACE = "org.bluez.Adapter1"


# --- Internal sync helpers (original dbus-python implementation) ---


def _get_root_proxy():
    return BUS.get_object(ORG_BLUEZ, ORG_BLUEZ_PATH)


def _get_adapter_proxy(hci):
    hci_path = os.path.join(ORG_BLUEZ_PATH, hci)
    return BUS.get_object(ORG_BLUEZ, hci_path)


def _get_device_proxy(hci, dev):
    device_path = os.path.join(ORG_BLUEZ_PATH, hci, dev)
    return BUS.get_object(ORG_BLUEZ, device_path)


def _introspect_tree(proxy):
    iface = dbus.Interface(proxy, "org.freedesktop.DBus.Introspectable")
    return ElementTree.fromstring(iface.Introspect())


def _get_node_child_names(proxy):
    tree = _introspect_tree(proxy)
    return [child.attrib["name"] for child in tree if child.tag == "node"]


def _get_node_interfaces(proxy):
    tree = _introspect_tree(proxy)
    return [child.attrib["name"] for child in tree if child.tag == "interface"]


def _get_adapter_attrib(proxy, attrib):
    iface = dbus.Interface(proxy, DBUS_PROPERTIES_INTERFACE)
    return iface.Get(BLUEZ_ADAPTER1_INTERFACE, attrib)


def _enable_adapter_sync(hci):
    """Set the HCI's Powered attribute to true (sync)."""
    proxy = _get_adapter_proxy(hci)
    iface = dbus.Interface(proxy, DBUS_PROPERTIES_INTERFACE)
    if iface.Get(BLUEZ_ADAPTER1_INTERFACE, "Powered").real:
        return False
    try:
        logger.debug("Enabling adapter")
        iface.Set(BLUEZ_ADAPTER1_INTERFACE, "Powered", True)
        return True
    except dbus.exceptions.DBusException as e:
        if "rfkill" in str(e):
            rfkill_unblock(hci)
            return _enable_adapter_sync(hci)
        raise


def _get_attached_addresses_sync(hci):
    """Get the addresses of devices known by hci (sync)."""
    proxy = _get_adapter_proxy(hci)
    devices = _get_node_child_names(proxy)

    known_devices = []
    for dev in devices:
        proxy2 = _get_device_proxy(hci, dev)
        dev_addr = str(_get_adapter_attrib_device(proxy2, "Address"))
        known_devices.append(dev_addr)

    return known_devices


def _get_adapter_attrib_device(proxy, attrib):
    """Get attribute from Bluez Device1 interface."""
    iface = dbus.Interface(proxy, DBUS_PROPERTIES_INTERFACE)
    return iface.Get("org.bluez.Device1", attrib)


def _get_hci_dict_sync():
    """Get dictionary mapping hci number to address (sync)."""
    proxy = _get_root_proxy()
    hcis = _get_node_child_names(proxy)
    hci_dict = {}

    for hci in hcis:
        proxy2 = _get_adapter_proxy(hci)
        interfaces = _get_node_interfaces(proxy2)
        if DBUS_PROPERTIES_INTERFACE not in interfaces or BLUEZ_ADAPTER1_INTERFACE not in interfaces:
            continue
        addr = _get_adapter_attrib(proxy2, "Address")
        hci_dict[hci] = str(addr)

    return hci_dict


# --- Public async API (matches bluetooth_modern.py) ---


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


async def enable_adapter(hci: str) -> bool:
    """Power on the Bluetooth adapter, unblocking rfkill if needed."""
    return await asyncio.to_thread(_enable_adapter_sync, hci)


async def get_attached_addresses(hci: str) -> list[str]:
    """Get MAC addresses of devices known to the adapter."""
    return await asyncio.to_thread(_get_attached_addresses_sync, hci)


async def get_hci_dict() -> dict[str, str]:
    """Get dictionary mapping hci name to Bluetooth address."""
    return await asyncio.to_thread(_get_hci_dict_sync)
