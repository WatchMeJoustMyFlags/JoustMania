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

``scripts/hardware/reset_bluetooth_connections.sh`` wraps this and restarts the
pairing daemon afterwards: the running daemon remembers controllers it already
verified in this session (``UsbPairingManager._verified_serials``) and would
not re-pair them over USB until it restarts. Exit code 1 when any removal
failed.
"""

import argparse
import asyncio
import configparser
import logging
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dbus_fast import Message, Variant

from lib.bluez_dbus import ORG_BLUEZ, ORG_BLUEZ_PATH, DBusError, assert_reply, get_managed_objects, get_system_bus

logger = logging.getLogger("psmove-pairing")

SONY_VENDOR_ID = 0x054C
# ZCM1 (0x03D5) and ZCM2/ZCM2E (0x0C5E). 0x042F is the PS Move Navigation
# controller, not a motion controller: deliberately excluded.
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


@dataclass(frozen=True)
class RemovalResult:
    """Outcome of removing one registration: ``removed``, ``already_absent`` or ``failed``."""

    registration: PsMoveRegistration
    outcome: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome != "failed"


@dataclass
class Scan:
    """PS Move registrations plus whether BlueZ could be asked (False: saved files only)."""

    registrations: list[PsMoveRegistration]
    bluez_reachable: bool


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


# bluetoothd is not running (nobody owns org.bluez): saved files are safe to edit.
_BLUEZ_ABSENT_ERRORS = frozenset(
    {"org.freedesktop.DBus.Error.ServiceUnknown", "org.freedesktop.DBus.Error.NameHasNoOwner"}
)


async def _bluez_objects() -> dict | None:
    """BlueZ's object tree, or None when bluetoothd / the system bus is absent.

    Only "nobody owns org.bluez" and a missing bus socket (OSError) mean absent.
    Any other D-Bus error (AccessDenied, NoReply from a busy bluetoothd, ...)
    propagates: bluetoothd may be running, so its storage must not be touched.
    """
    try:
        bus = await get_system_bus()
        return await get_managed_objects(bus)
    except DBusError as error:
        if error.error_name in _BLUEZ_ABSENT_ERRORS:
            return None
        raise
    except OSError:
        return None


async def scan_psmove_registrations(bluetooth_dir: Path = DEFAULT_BLUETOOTH_DIR) -> Scan:
    """Find PS Move registrations: live BlueZ devices plus saved-only directories.

    Saved-only directories are considered only for adapters BlueZ does not
    present right now (BlueZ owns the storage of present adapters). When BlueZ
    is unreachable, every saved PS Move directory is considered (bluetoothd
    reads them only at startup, so deleting them while it is down is safe).
    """
    objects = await _bluez_objects()
    if objects is None:
        return Scan(_saved_only_registrations(bluetooth_dir, present_adapters=set()), bluez_reachable=False)

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

    present = {address for _, address in adapters.values()}
    found.extend(_saved_only_registrations(bluetooth_dir, present_adapters=present))
    return Scan(found, bluez_reachable=True)


async def find_psmove_registrations(bluetooth_dir: Path = DEFAULT_BLUETOOTH_DIR) -> list[PsMoveRegistration]:
    """List PS Move registrations (see :func:`scan_psmove_registrations`)."""
    return (await scan_psmove_registrations(bluetooth_dir)).registrations


def _saved_only_registrations(bluetooth_dir: Path, present_adapters: set[str]) -> list[PsMoveRegistration]:
    """PS Move registrations saved on disk under adapters BlueZ does not present (e.g. unplugged)."""
    if not bluetooth_dir.is_dir():
        return []
    saved: list[PsMoveRegistration] = []
    for info_path in sorted(bluetooth_dir.glob("*/*/info")):
        controller_dir = info_path.parent
        adapter_address = controller_dir.parent.name.upper()
        address = controller_dir.name.upper()
        if not (_MAC_RE.fullmatch(adapter_address) and _MAC_RE.fullmatch(address)):
            continue
        if adapter_address in present_adapters or not _saved_registration_is_psmove(info_path):
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


async def _remove_one(registration: PsMoveRegistration) -> RemovalResult:
    if registration.device_path is not None:
        try:
            await _remove_device(registration.device_path.rsplit("/", 1)[0], registration.device_path)
        except DBusError as error:
            if error.error_name == "org.bluez.Error.DoesNotExist":
                return RemovalResult(registration, "already_absent")
            return RemovalResult(registration, "failed", f"{error.error_name}: {error}")
        except (OSError, ConnectionError) as error:
            return RemovalResult(registration, "failed", str(error))
        return RemovalResult(registration, "removed")
    if registration.saved_dir is not None:
        try:
            shutil.rmtree(registration.saved_dir)
        except FileNotFoundError:
            return RemovalResult(registration, "already_absent")
        except OSError as error:
            return RemovalResult(registration, "failed", str(error))
        return RemovalResult(registration, "removed")
    return RemovalResult(registration, "failed", "registration has neither a BlueZ path nor a saved directory")


async def clear_psmove_registrations(
    registrations: list[PsMoveRegistration],
    on_result: Callable[[RemovalResult], None] | None = None,
) -> list[RemovalResult]:
    """Remove the given PS Move registrations, each one independently.

    Live devices go through ``Adapter1.RemoveDevice`` (BlueZ also deletes the
    saved directory); saved-only directories are deleted from disk. A failure
    on one registration does not stop the others; ``on_result`` sees every
    outcome as it happens.
    """
    results = []
    for registration in registrations:
        result = await _remove_one(registration)
        if on_result is not None:
            on_result(result)
        results.append(result)
    return results


# --- One controller on every adapter: unpair (UC-005) and re-pair cleanup (UC-001 BR-004) ---


def normalize_address(address: str) -> str | None:
    """Upper-case ``address`` with ':' separators, or None if it is not a Bluetooth address."""
    candidate = address.strip().upper().replace("-", ":")
    return candidate if _MAC_RE.fullmatch(candidate) else None


def _has_identity(properties: dict) -> bool:
    """Whether BlueZ knows what kind of device this is (Modalias or device class)."""
    return bool(_plain(properties.get("Modalias"))) or bool(_plain(properties.get("Class")))


def _saved_has_device_id(info_path: Path) -> bool:
    config = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        config.read(info_path)
    except (configparser.Error, OSError):
        return True  # unreadable: treat as identified, so it is not removed blindly
    return config.has_section("DeviceID")


def _saved_of_address(
    bluetooth_dir: Path, address: str, present_adapters: set[str], accept: Callable[[Path], bool]
) -> list[PsMoveRegistration]:
    """Saved directories of one address under adapters BlueZ does not present."""
    if not bluetooth_dir.is_dir():
        return []
    saved: list[PsMoveRegistration] = []
    for info_path in sorted(bluetooth_dir.glob(f"*/{address}/info")):
        controller_dir = info_path.parent
        adapter_address = controller_dir.parent.name.upper()
        if _MAC_RE.fullmatch(adapter_address) and adapter_address not in present_adapters and accept(info_path):
            saved.append(PsMoveRegistration(adapter_address, address, saved_dir=controller_dir))
    return saved


async def find_controller_registrations(
    address: str,
    *,
    keep_adapter: str | None = None,
    require_psmove: bool = True,
    bluetooth_dir: Path | None = None,
) -> list[PsMoveRegistration]:
    """Registrations of one controller address on every adapter but ``keep_adapter``.

    ``require_psmove`` (the operator unpair, UC-005 BR-001): only when at least one
    registration of the address is a PS Move are any of them taken; then copies of
    the same address that carry no device identity (never connected: no Modalias,
    no class, no saved ``[DeviceID]``) are taken too, while a copy identified as
    another device never is. Without ``require_psmove`` (the re-pair cleanup,
    UC-001 BR-004) the address alone decides: it was read from a PS Move on USB.

    Raises:
        ValueError: ``address`` is not a Bluetooth address.
        DBusError: BlueZ answered with an error other than "not running".
    """
    normalized = normalize_address(address)
    if normalized is None:
        raise ValueError(f"not a Bluetooth address: {address!r}")
    keep = normalize_address(keep_adapter) if keep_adapter else None
    bluetooth_dir = bluetooth_dir if bluetooth_dir is not None else DEFAULT_BLUETOOTH_DIR

    objects = await _bluez_objects()
    adapters: dict[str, tuple[str, str]] = {}
    live: list[tuple[PsMoveRegistration, dict]] = []
    if objects is not None:
        for path, interfaces in objects.items():
            props = interfaces.get("org.bluez.Adapter1")
            if props is not None and path.startswith(ORG_BLUEZ_PATH + "/"):
                adapters[path] = (path.rsplit("/", 1)[-1], str(_plain(props.get("Address", ""))).upper())
        for path, interfaces in objects.items():
            props = interfaces.get("org.bluez.Device1")
            adapter = adapters.get(path.rsplit("/", 1)[0])
            if props is None or adapter is None or str(_plain(props.get("Address", ""))).upper() != normalized:
                continue
            live.append((PsMoveRegistration(adapter[1], normalized, hci=adapter[0], device_path=path), props))
    present = {address for _, address in adapters.values()}

    if require_psmove:
        live_move = any(is_psmove_device(props) for _, props in live)
        saved_move = _saved_of_address(bluetooth_dir, normalized, present, _saved_registration_is_psmove)
        if not (live_move or saved_move):
            return []
        found = [r for r, props in live if is_psmove_device(props) or not _has_identity(props)]
        found += saved_move + _saved_of_address(
            bluetooth_dir, normalized, present, lambda info: not _saved_has_device_id(info)
        )
    else:
        found = [r for r, _ in live] + _saved_of_address(bluetooth_dir, normalized, present, lambda _info: True)
    return [r for r in found if r.adapter_address != keep]


def _log_result(result: RemovalResult) -> None:
    where = f"{result.registration.address} on adapter {result.registration.adapter_address}"
    if result.outcome == "failed":
        logger.warning(f"FAILED to remove registration of {where}: {result.error}")
    else:
        logger.info(f"Registration of {where}: {result.outcome}")


async def remove_controller_registrations(
    address: str,
    *,
    keep_adapter: str | None = None,
    require_psmove: bool = True,
    bluetooth_dir: Path | None = None,
) -> list[RemovalResult]:
    """Remove one controller's registrations on every adapter (UC-005, UC-001 BR-004).

    Returns one :class:`RemovalResult` per registration found (empty: none found).
    Raises like :func:`find_controller_registrations`.
    """
    found = await find_controller_registrations(
        address, keep_adapter=keep_adapter, require_psmove=require_psmove, bluetooth_dir=bluetooth_dir
    )
    return await clear_psmove_registrations(found, on_result=_log_result)


def _where(registration: PsMoveRegistration) -> str:
    if registration.device_path is not None:
        return f"PS Move {registration.address} from {registration.hci}"
    return f"saved PS Move {registration.address} from adapter {registration.adapter_address}"


def _print_result(result: RemovalResult) -> None:
    where = _where(result.registration)
    if result.outcome == "removed":
        print(f"Removed {where}", flush=True)
    elif result.outcome == "already_absent":
        print(f"Already absent: {where}", flush=True)
    else:
        print(f"FAILED to remove {where}: {result.error}", file=sys.stderr, flush=True)


async def async_main(argv: list[str] | None = None) -> int:
    """CLI entry point. Exit code 0 when every registration is gone, 1 on any failure."""
    parser = argparse.ArgumentParser(
        description="Remove PS Move Bluetooth registrations (other Bluetooth devices are left alone)."
    )
    parser.add_argument("--dry-run", action="store_true", help="list what would be removed, change nothing")
    parser.add_argument(
        "--bluetooth-dir", type=Path, default=DEFAULT_BLUETOOTH_DIR, help="BlueZ storage (default /var/lib/bluetooth)"
    )
    args = parser.parse_args(argv)

    try:
        scan = await scan_psmove_registrations(args.bluetooth_dir)
    except DBusError as error:
        print(f"FAILED to query BlueZ ({error.error_name}: {error}); nothing removed", file=sys.stderr, flush=True)
        return 1
    if not scan.bluez_reachable:
        print("BlueZ is not reachable: only saved registrations under the storage directory are handled", flush=True)

    if args.dry_run:
        for registration in scan.registrations:
            print(f"Would remove {_where(registration)}", flush=True)
        print(f"Found {len(scan.registrations)} PS Move registration(s)")
        return 0

    results = await clear_psmove_registrations(scan.registrations, on_result=_print_result)
    removed = sum(r.outcome == "removed" for r in results)
    absent = sum(r.outcome == "already_absent" for r in results)
    failed = sum(not r.ok for r in results)
    print(f"Removed {removed}, already absent {absent}, failed {failed} PS Move registration(s)")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
