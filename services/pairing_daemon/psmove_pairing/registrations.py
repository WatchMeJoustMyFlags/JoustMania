"""Find and remove PS Move Bluetooth registrations, and nothing else (C-011, #1429).

A registration is either a live BlueZ ``org.bluez.Device1`` object or a saved
directory ``/var/lib/bluetooth/<adapter>/<controller>/`` that BlueZ never
loaded (for example because that adapter is unplugged). Only PS Moves are
touched: Sony vendor 054c with product 03d5 (ZCM1) or 0c5e (ZCM2/ZCM2E);
keyboards, headsets and other Bluetooth devices stay registered.

Run inside the pairing-daemon container (it has the system D-Bus and
/var/lib/bluetooth mounted)::

    python -m psmove_pairing.registrations --dry-run
    python -m psmove_pairing.registrations

``scripts/hardware/reset_bluetooth_connections.sh`` wraps this.
"""

import argparse
import asyncio
import configparser
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from dbus_fast import Message, Variant

from lib.bluez_dbus import ORG_BLUEZ, ORG_BLUEZ_PATH, assert_reply, get_managed_objects, get_system_bus

SONY_VENDOR_ID = 0x054C
PS_MOVE_PRODUCT_IDS = (0x03D5, 0x0C5E)

# Fallback when BlueZ has no Modalias yet: all three must match.
PS_MOVE_NAME = "Motion Controller"
PS_MOVE_CLASS = 0x002508
HID_SERVICE_UUID = "00001124-0000-1000-8000-00805f9b34fb"

DEFAULT_BLUETOOTH_DIR = Path("/var/lib/bluetooth")

_MAC_RE = re.compile(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}")
_MODALIAS_RE = re.compile(r"v([0-9a-f]{4})p([0-9a-f]{4})")


@dataclass(frozen=True)
class PsMoveRegistration:
    """One PS Move registration on one adapter."""

    adapter_address: str
    address: str
    hci: str | None = None  # None for a saved-only registration of an absent adapter
    device_path: str | None = None  # BlueZ object path when BlueZ has it loaded
    saved_dir: Path | None = None  # set for saved-only registrations


def _plain(value):
    return value.value if isinstance(value, Variant) else value


def is_psmove_device(properties: dict) -> bool:
    """Whether BlueZ Device1 properties describe a PS Move.

    Primary check: the Modalias carries Sony's vendor id and a PS Move product id.
    Fallback (no Modalias): name, device class and HID service UUID all match.
    """
    modalias = str(_plain(properties.get("Modalias", ""))).lower()
    match = _MODALIAS_RE.search(modalias)
    if match:
        vendor, product = (int(part, 16) for part in match.groups())
        return vendor == SONY_VENDOR_ID and product in PS_MOVE_PRODUCT_IDS

    name = str(_plain(properties.get("Name", properties.get("Alias", ""))))
    device_class = int(_plain(properties.get("Class", 0)) or 0)
    uuids = [str(uuid).lower() for uuid in _plain(properties.get("UUIDs", [])) or []]
    return name == PS_MOVE_NAME and device_class == PS_MOVE_CLASS and HID_SERVICE_UUID in uuids


def _saved_registration_is_psmove(info_path: Path) -> bool:
    """Whether a saved BlueZ ``info`` file has a PS Move [DeviceID] section."""
    config = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        config.read(info_path)
        vendor = config.getint("DeviceID", "Vendor", fallback=0)
        product = config.getint("DeviceID", "Product", fallback=0)
    except (configparser.Error, OSError, ValueError):
        return False
    return vendor == SONY_VENDOR_ID and product in PS_MOVE_PRODUCT_IDS


async def find_psmove_registrations(bluetooth_dir: Path = DEFAULT_BLUETOOTH_DIR) -> list[PsMoveRegistration]:
    """List PS Move registrations: live BlueZ devices plus saved-only directories."""
    bus = await get_system_bus()
    objects = await get_managed_objects(bus)

    adapters: dict[str, tuple[str, str]] = {}  # object path -> (hci, address)
    for path, interfaces in objects.items():
        props = interfaces.get("org.bluez.Adapter1")
        if props is not None and path.startswith(ORG_BLUEZ_PATH + "/"):
            adapters[path] = (path.rsplit("/", 1)[-1], str(_plain(props.get("Address", ""))).upper())

    found: list[PsMoveRegistration] = []
    for path, interfaces in objects.items():
        props = interfaces.get("org.bluez.Device1")
        if props is None or not is_psmove_device(props):
            continue
        adapter_path = path.rsplit("/", 1)[0]
        if adapter_path not in adapters:
            continue
        hci, adapter_address = adapters[adapter_path]
        address = str(_plain(props.get("Address", ""))).upper()
        found.append(PsMoveRegistration(adapter_address, address, hci=hci, device_path=path))

    live = {(r.adapter_address, r.address) for r in found}
    found.extend(_saved_only_registrations(bluetooth_dir, live))
    return found


def _saved_only_registrations(bluetooth_dir: Path, live: set[tuple[str, str]]) -> list[PsMoveRegistration]:
    """PS Move registrations saved on disk that BlueZ has not loaded (e.g. adapter unplugged)."""
    if not bluetooth_dir.is_dir():
        return []
    saved: list[PsMoveRegistration] = []
    for info_path in sorted(bluetooth_dir.glob("*/*/info")):
        controller_dir = info_path.parent
        adapter_address = controller_dir.parent.name.upper()
        address = controller_dir.name.upper()
        if not (_MAC_RE.fullmatch(adapter_address) and _MAC_RE.fullmatch(address)):
            continue
        if (adapter_address, address) in live or not _saved_registration_is_psmove(info_path):
            continue
        saved.append(PsMoveRegistration(adapter_address, address, saved_dir=controller_dir))
    return saved


async def _remove_device(adapter_path: str, device_path: str) -> None:
    bus = await get_system_bus()
    assert_reply(
        await bus.call(
            Message(
                destination=ORG_BLUEZ,
                path=adapter_path,
                interface="org.bluez.Adapter1",
                member="RemoveDevice",
                signature="o",
                body=[device_path],
            )
        )
    )


async def clear_psmove_registrations(
    dry_run: bool = False, bluetooth_dir: Path = DEFAULT_BLUETOOTH_DIR
) -> list[PsMoveRegistration]:
    """Remove every PS Move registration (or only list them when ``dry_run``).

    Live devices go through ``Adapter1.RemoveDevice`` (BlueZ also deletes the
    saved directory); saved-only directories are deleted from disk.
    """
    registrations = await find_psmove_registrations(bluetooth_dir)
    if dry_run:
        return registrations
    for registration in registrations:
        if registration.device_path is not None:
            await _remove_device(registration.device_path.rsplit("/", 1)[0], registration.device_path)
        elif registration.saved_dir is not None:
            shutil.rmtree(registration.saved_dir, ignore_errors=True)
    return registrations


def _describe(registration: PsMoveRegistration, dry_run: bool) -> str:
    action = "Would remove" if dry_run else "Removed"
    if registration.device_path is not None:
        return f"{action} PS Move {registration.address} from {registration.hci}"
    return f"{action} saved PS Move {registration.address} from adapter {registration.adapter_address}"


async def async_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remove PS Move Bluetooth registrations (other Bluetooth devices are left alone)."
    )
    parser.add_argument("--dry-run", action="store_true", help="list what would be removed, change nothing")
    parser.add_argument(
        "--bluetooth-dir", type=Path, default=DEFAULT_BLUETOOTH_DIR, help="BlueZ storage (default /var/lib/bluetooth)"
    )
    args = parser.parse_args(argv)

    registrations = await clear_psmove_registrations(dry_run=args.dry_run, bluetooth_dir=args.bluetooth_dir)
    for registration in registrations:
        print(_describe(registration, args.dry_run))
    verb = "Found" if args.dry_run else "Removed"
    print(f"{verb} {len(registrations)} PS Move registration(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
