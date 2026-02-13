"""Bluetooth adapter management using dbus-python (legacy backend).

Wraps synchronous dbus-python calls in asyncio.to_thread() to provide the same
async API as the modern dbus-fast backend.
"""

import asyncio
import logging
from dataclasses import dataclass
from xml.etree import ElementTree

import dbus

logger = logging.getLogger("psmove-pairing")

ORG_BLUEZ = "org.bluez"
ORG_BLUEZ_PATH = "/org/bluez"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
BLUEZ_ADAPTER1_INTERFACE = "org.bluez.Adapter1"

_BUS = None


def _get_bus():
    """Lazily connect to the DBus system bus on first use."""
    global _BUS
    if _BUS is None:
        _BUS = dbus.SystemBus()
    return _BUS


@dataclass
class AdapterInfo:
    """Information about a Bluetooth adapter."""

    hci: str
    address: str
    name: str
    device_count: int


# --- Internal sync helpers ---


def _get_root_proxy():
    return _get_bus().get_object(ORG_BLUEZ, ORG_BLUEZ_PATH)


def _get_adapter_proxy(hci: str):
    import os

    hci_path = os.path.join(ORG_BLUEZ_PATH, hci)
    return _get_bus().get_object(ORG_BLUEZ, hci_path)


def _introspect_tree(proxy):
    iface = dbus.Interface(proxy, "org.freedesktop.DBus.Introspectable")
    return ElementTree.fromstring(iface.Introspect())


def _get_node_child_names(proxy) -> list[str]:
    tree = _introspect_tree(proxy)
    return [child.attrib["name"] for child in tree if child.tag == "node"]


def _get_node_interfaces(proxy) -> list[str]:
    tree = _introspect_tree(proxy)
    return [child.attrib["name"] for child in tree if child.tag == "interface"]


def _get_adapter_attrib(proxy, attrib: str):
    iface = dbus.Interface(proxy, DBUS_PROPERTIES_INTERFACE)
    return iface.Get(BLUEZ_ADAPTER1_INTERFACE, attrib)


def _get_hci_dict_sync() -> dict[str, str]:
    """Get dictionary mapping hci name to Bluetooth address (sync)."""
    proxy = _get_root_proxy()
    hcis = _get_node_child_names(proxy)
    hci_dict = {}

    for hci in hcis:
        try:
            proxy2 = _get_adapter_proxy(hci)
            interfaces = _get_node_interfaces(proxy2)
            if DBUS_PROPERTIES_INTERFACE not in interfaces or BLUEZ_ADAPTER1_INTERFACE not in interfaces:
                continue
            addr = _get_adapter_attrib(proxy2, "Address")
            hci_dict[hci] = str(addr)
        except dbus.exceptions.DBusException as e:
            logger.debug(f"Error getting adapter {hci}: {e}")
            continue

    return hci_dict


def _get_attached_addresses_sync(hci: str) -> list[str]:
    """Get the addresses of devices known by an HCI adapter (sync)."""
    try:
        proxy = _get_adapter_proxy(hci)
        devices = _get_node_child_names(proxy)

        known_devices = []
        for dev in devices:
            try:
                import os

                device_path = os.path.join(ORG_BLUEZ_PATH, hci, dev)
                dev_proxy = _get_bus().get_object(ORG_BLUEZ, device_path)
                iface = dbus.Interface(dev_proxy, DBUS_PROPERTIES_INTERFACE)
                dev_addr = str(iface.Get("org.bluez.Device1", "Address"))
                known_devices.append(dev_addr)
            except dbus.exceptions.DBusException:
                continue

        return known_devices
    except dbus.exceptions.DBusException as e:
        logger.debug(f"Error getting devices for {hci}: {e}")
        return []


def _refresh_adapters_sync(hci_dict_ref, bt_devices_ref):
    """Refresh adapters synchronously, updating the provided dicts in-place."""
    hci_dict = _get_hci_dict_sync()
    hci_dict_ref.clear()
    hci_dict_ref.update(hci_dict)

    bt_devices_ref.clear()
    for hci, addr in hci_dict.items():
        devices = _get_attached_addresses_sync(hci)
        bt_devices_ref[addr] = devices
        logger.debug(f"Adapter {addr} ({hci}): {len(devices)} devices")

    return dict(bt_devices_ref)


async def restart_systemd_unit(unit: str, mode: str = "replace") -> None:
    """Restart a systemd unit via the dbus-python systemd interface."""

    def _restart_sync():
        bus = dbus.SystemBus()
        systemd = bus.get_object("org.freedesktop.systemd1", "/org/freedesktop/systemd1")
        manager = dbus.Interface(systemd, "org.freedesktop.systemd1.Manager")
        manager.RestartUnit(unit, mode)

    await asyncio.to_thread(_restart_sync)


# --- Public async API ---


async def get_hci_dict() -> dict[str, str]:
    """Get dictionary mapping hci name to Bluetooth address."""
    return await asyncio.to_thread(_get_hci_dict_sync)


async def get_attached_addresses(hci: str) -> list[str]:
    """Get the addresses of devices known by an HCI adapter."""
    return await asyncio.to_thread(_get_attached_addresses_sync, hci)


class AdapterManager:
    """Manages Bluetooth adapters using dbus-python (legacy)."""

    def __init__(self):
        self._hci_dict: dict[str, str] = {}
        self._bt_devices: dict[str, list[str]] = {}

    async def refresh_adapters(self) -> dict[str, list[str]]:
        """Refresh the list of adapters and their paired devices."""
        return await asyncio.to_thread(_refresh_adapters_sync, self._hci_dict, self._bt_devices)

    def get_lowest_bt_device(self) -> str:
        """Get the address of the adapter with the fewest paired devices."""
        if not self._bt_devices:
            logger.warning("No Bluetooth adapters found")
            return ""

        min_count = min(len(devices) for devices in self._bt_devices.values())
        for addr in sorted(self._bt_devices.keys()):
            if len(self._bt_devices[addr]) == min_count:
                logger.info(f"Selected adapter {addr} with {min_count} devices (of {len(self._bt_devices)} adapters)")
                return addr
        return ""

    def select_least_loaded_adapter(self) -> AdapterInfo | None:
        """Select the adapter with the fewest paired devices."""
        if not self._bt_devices:
            logger.warning("No Bluetooth adapters found")
            return None

        min_count = min(len(devices) for devices in self._bt_devices.values())
        for addr in sorted(self._bt_devices.keys()):
            if len(self._bt_devices[addr]) == min_count:
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
                for a, devs in sorted(self._bt_devices.items()):
                    logger.debug(f"  {a}: {len(devs)} devices")
                return adapter
        return None

    def check_if_not_paired(self, controller_addr: str) -> bool:
        """Check if a controller is not yet paired to any adapter."""
        controller_upper = controller_addr.upper()
        return all(controller_upper not in [d.upper() for d in devices] for devices in self._bt_devices.values())
