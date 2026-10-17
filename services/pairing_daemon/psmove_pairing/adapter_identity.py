"""Bluetooth adapter identity, HCI error counters and link roles (UC-035 BR-004, #1431).

Read-only: parses ``hciconfig -a`` and ``hcitool con`` output and looks up the
USB device above each HCI interface in sysfs. Chipsets are identified by the HCI
manufacturer (e.g. Realtek 93, Cambridge Silicon Radio 10, Broadcom 15, Cypress
305), not by brand. Nothing here changes a role or an adapter setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "unknown"

SYS_CLASS_BLUETOOTH = Path("/sys/class/bluetooth")
DEVICE_TREE = Path("/sys/firmware/devicetree/base")

_ADAPTER_HEADER = re.compile(r"^(hci\d+):")
_BUS = re.compile(r"Bus:\s*(\S+)")
_ADDRESS = re.compile(r"BD Address:\s*([0-9A-Fa-f:]{17})")
_HCI_VERSION = re.compile(r"HCI Version:\s*([0-9.]+)")
_MANUFACTURER = re.compile(r"Manufacturer:\s*(.+?)\s*\((\d+)\)\s*$", re.M)
_RX_ERRORS = re.compile(r"RX bytes:\d+.*?errors:(\d+)")
_TX_ERRORS = re.compile(r"TX bytes:\d+.*?errors:(\d+)")
_MAC = re.compile(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
_LINK_MODE = re.compile(r"\blm\s+(\w+)")

# hcitool reports the local adapter's role in the link mode ("lm") field.
_ROLES = {"MASTER": "central", "CENTRAL": "central", "SLAVE": "peripheral", "PERIPHERAL": "peripheral"}


@dataclass(frozen=True)
class AdapterIdentity:
    hci: str
    address: str = UNKNOWN
    manufacturer: str = UNKNOWN
    manufacturer_id: str = UNKNOWN
    hci_version: str = UNKNOWN
    bus: str = UNKNOWN
    usb_id: str = UNKNOWN
    onboard: bool = False
    rx_errors: int | None = None
    tx_errors: int | None = None

    def labels(self) -> dict[str, str]:
        """Label set of pairing_bluetooth_adapter_info (error counters excluded)."""
        return {
            "hci_adapter": self.hci,
            "address": self.address,
            "manufacturer": self.manufacturer,
            "manufacturer_id": self.manufacturer_id,
            "hci_version": self.hci_version,
            "bus": self.bus,
            "usb_id": self.usb_id,
            "onboard": "true" if self.onboard else "false",
        }


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip().rstrip("\0")
    except OSError:
        return ""


def usb_id(hci: str, sys_class: Path = SYS_CLASS_BLUETOOTH) -> str:
    """``vendor:product`` of the USB device above ``hci``, or ``unknown``."""
    try:
        device = (sys_class / hci / "device").resolve()
    except (OSError, RuntimeError):
        return UNKNOWN
    for parent in (device, *device.parents):
        vendor = _read(parent / "idVendor")
        product = _read(parent / "idProduct")
        if vendor and product:
            return f"{vendor}:{product}".lower()
    return UNKNOWN


def is_devicetree_radio(hci: str, sys_class: Path = SYS_CLASS_BLUETOOTH, device_tree: Path = DEVICE_TREE) -> bool:
    """True when ``hci`` is the Raspberry Pi's device-tree Bluetooth node (whatever its hci number)."""
    alias = _read(device_tree / "aliases" / "bluetooth")
    if not alias or not _read(device_tree / "model").startswith("Raspberry Pi"):
        return False
    try:
        expected = (device_tree / alias.lstrip("/")).resolve(strict=True)
        device = (sys_class / hci / "device").resolve(strict=True)
        return any((parent / "of_node").resolve() == expected for parent in (device, *device.parents))
    except (OSError, RuntimeError):
        return False


def parse_hciconfig(
    output: str, sys_class: Path = SYS_CLASS_BLUETOOTH, device_tree: Path = DEVICE_TREE
) -> list[AdapterIdentity]:
    """Parse ``hciconfig -a`` into one identity per adapter.

    A DOWN adapter prints no version or manufacturer; those fields stay ``unknown``.
    ``onboard`` is true for a non-USB bus (UART, SDIO) or the Pi's device-tree radio.
    """
    adapters = []
    for block in re.split(r"(?m)(?=^hci\d+:)", output):
        header = _ADAPTER_HEADER.match(block)
        if not header:
            continue
        hci = header.group(1)
        bus = _BUS.search(block)
        address = _ADDRESS.search(block)
        version = _HCI_VERSION.search(block)
        manufacturer = _MANUFACTURER.search(block)
        rx = _RX_ERRORS.search(block)
        tx = _TX_ERRORS.search(block)
        bus_name = bus.group(1).upper() if bus else UNKNOWN
        adapters.append(
            AdapterIdentity(
                hci=hci,
                address=address.group(1).upper() if address else UNKNOWN,
                manufacturer=manufacturer.group(1) if manufacturer else UNKNOWN,
                manufacturer_id=manufacturer.group(2) if manufacturer else UNKNOWN,
                hci_version=version.group(1) if version else UNKNOWN,
                bus=bus_name,
                usb_id=usb_id(hci, sys_class),
                onboard=(bus_name not in ("USB", UNKNOWN)) or is_devicetree_radio(hci, sys_class, device_tree),
                rx_errors=int(rx.group(1)) if rx else None,
                tx_errors=int(tx.group(1)) if tx else None,
            )
        )
    return adapters


def parse_hcitool_con(output: str) -> list[tuple[str, str]]:
    """``(lowercase mac, role)`` per link from ``hcitool con``; role is the local adapter's."""
    links = []
    for line in output.split("\n"):
        mac = _MAC.search(line)
        if not mac:
            continue
        mode = _LINK_MODE.search(line)
        role = _ROLES.get(mode.group(1).upper(), UNKNOWN) if mode else UNKNOWN
        links.append((mac.group(0).lower(), role))
    return links


class HciErrorTracker:
    """Turns the kernel's cumulative HCI error counters into counter increments."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], int] = {}

    def delta(self, hci: str, direction: str, value: int) -> int:
        """Increase since the last reading; the first reading is the baseline (0).

        A drop (adapter reset) counts the new value, since the kernel restarted from 0.
        """
        key = (hci, direction)
        last = self._last.get(key)
        self._last[key] = value
        if last is None:
            return 0
        return value - last if value >= last else value
