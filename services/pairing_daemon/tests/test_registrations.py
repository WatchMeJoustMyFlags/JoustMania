"""Tests for the PS Move registration reset (C-011, #1429).

The reset must remove only PS Move registrations (Sony vendor 054c, product
03d5 / 0c5e): keyboards, headsets and other BlueZ devices stay untouched.
The BlueZ object tree and D-Bus calls are faked; saved registrations use a
temporary /var/lib/bluetooth layout.
"""

from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from psmove_pairing import registrations
from psmove_pairing.registrations import (
    clear_psmove_registrations,
    find_psmove_registrations,
    is_psmove_device,
)

HCI0 = "AA:00:00:00:00:00"
HCI1 = "BB:00:00:00:00:00"
GONE_ADAPTER = "CC:00:00:00:00:00"

ZCM1 = "00:06:F7:00:00:01"
ZCM2 = "00:06:F7:00:00:02"
FALLBACK_MOVE = "00:06:F7:00:00:03"
KEYBOARD = "11:22:33:44:55:66"
HEADSET = "22:33:44:55:66:77"
NAME_ONLY = "33:44:55:66:77:88"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"


def device(address: str, **props) -> dict:
    values = {"Address": Variant("s", address)}
    for key, value in props.items():
        if key == "Class":
            values[key] = Variant("u", value)
        elif key == "UUIDs":
            values[key] = Variant("as", value)
        else:
            values[key] = Variant("s", value)
    return {"org.bluez.Device1": values}


def tree() -> dict:
    """hci0: ZCM1, keyboard, headset; hci1: ZCM2, fallback Move, name-only impostor."""
    return {
        "/org/bluez": {"org.bluez.AgentManager1": {}},
        "/org/bluez/hci0": {"org.bluez.Adapter1": {"Address": Variant("s", HCI0)}},
        "/org/bluez/hci1": {"org.bluez.Adapter1": {"Address": Variant("s", HCI1)}},
        "/org/bluez/hci0/dev_00_06_F7_00_00_01": device(ZCM1, Name="Motion Controller", Modalias="usb:v054Cp03D5d0001"),
        "/org/bluez/hci0/dev_11_22_33_44_55_66": device(
            KEYBOARD, Name="Keyboard", Modalias="usb:v05ACp0255d0100", Class=0x002540, UUIDs=[HID_UUID]
        ),
        "/org/bluez/hci0/dev_22_33_44_55_66_77": device(HEADSET, Name="Headset", Class=0x240404),
        "/org/bluez/hci1/dev_00_06_F7_00_00_02": device(ZCM2, Name="Motion Controller", Modalias="usb:v054Cp0C5Ed0001"),
        # No Modalias (never resolved); name, class and HID UUID all match a Move.
        "/org/bluez/hci1/dev_00_06_F7_00_00_03": device(
            FALLBACK_MOVE, Name="Motion Controller", Class=0x002508, UUIDs=[HID_UUID]
        ),
        # Same name but not a Move's class: not removed.
        "/org/bluez/hci1/dev_33_44_55_66_77_88": device(NAME_ONLY, Name="Motion Controller", Class=0x002540),
    }


def write_info(root: Path, adapter: str, address: str, vendor: int | None, product: int | None) -> Path:
    directory = root / adapter / address
    directory.mkdir(parents=True)
    lines = ["[General]", "Name=Saved device", "Trusted=true", ""]
    if vendor is not None:
        lines += ["[DeviceID]", "Source=2", f"Vendor={vendor}", f"Product={product}", "Version=1", ""]
    (directory / "info").write_text("\n".join(lines))
    return directory


@pytest.fixture
def bluetooth_dir(tmp_path: Path) -> Path:
    root = tmp_path / "bluetooth"
    root.mkdir()
    return root


@pytest.fixture
def fake_bus():
    """Patch the BlueZ helpers; the bus records every RemoveDevice call."""
    bus = create_autospec(MessageBus, instance=True)
    reply = create_autospec(Message, instance=True)
    reply.message_type = MessageType.METHOD_RETURN
    bus.call.return_value = reply
    with (
        patch.object(registrations, "get_system_bus", AsyncMock(return_value=bus)),
        patch.object(registrations, "get_managed_objects", AsyncMock(return_value=tree())),
    ):
        yield bus


def removed_paths(bus) -> list[tuple[str, str]]:
    calls = []
    for call in bus.call.await_args_list:
        message = call.args[0]
        assert (message.interface, message.member, message.signature) == ("org.bluez.Adapter1", "RemoveDevice", "o")
        calls.append((message.path, message.body[0]))
    return calls


class TestIsPsMoveDevice:
    def test_sony_modalias_zcm1_and_zcm2(self):
        assert is_psmove_device({"Modalias": "usb:v054Cp03D5d0001"})
        assert is_psmove_device({"Modalias": "bluetooth:v054Cp0C5Ed0100"})

    def test_other_sony_product_is_not_a_move(self):
        # DualShock 4 (054c:05c4)
        assert not is_psmove_device({"Name": "Wireless Controller", "Modalias": "usb:v054Cp05C4d0100"})

    def test_metadata_fallback_needs_name_class_and_hid_uuid(self):
        props = {"Name": "Motion Controller", "Class": 0x002508, "UUIDs": [HID_UUID.upper()]}
        assert is_psmove_device(props)
        assert not is_psmove_device({**props, "Class": 0x002540})
        assert not is_psmove_device({**props, "UUIDs": []})
        assert not is_psmove_device({**props, "Name": "Keyboard"})

    def test_empty_properties(self):
        assert not is_psmove_device({})


class TestClearPsMoveRegistrations:
    async def test_removes_only_ps_move_devices(self, fake_bus, bluetooth_dir):
        """Keyboards, headsets and name-only look-alikes are never removed."""
        removed = await clear_psmove_registrations(dry_run=False, bluetooth_dir=bluetooth_dir)

        assert sorted(r.address for r in removed) == [ZCM1, ZCM2, FALLBACK_MOVE]
        assert sorted(removed_paths(fake_bus)) == [
            ("/org/bluez/hci0", "/org/bluez/hci0/dev_00_06_F7_00_00_01"),
            ("/org/bluez/hci1", "/org/bluez/hci1/dev_00_06_F7_00_00_02"),
            ("/org/bluez/hci1", "/org/bluez/hci1/dev_00_06_F7_00_00_03"),
        ]
        touched = {body for _, body in removed_paths(fake_bus)}
        for other in (KEYBOARD, HEADSET, NAME_ONLY):
            assert not any(other.replace(":", "_") in path for path in touched)

    async def test_dry_run_changes_nothing(self, fake_bus, bluetooth_dir):
        saved = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:09", 0x054C, 0x03D5)

        found = await clear_psmove_registrations(dry_run=True, bluetooth_dir=bluetooth_dir)

        assert len(found) == 4
        fake_bus.call.assert_not_awaited()
        assert saved.exists()

    async def test_saved_only_registrations_removed_only_for_ps_moves(self, fake_bus, bluetooth_dir):
        """Registrations of an unplugged adapter are invisible to BlueZ; only Sony Move ones are deleted."""
        saved_zcm1 = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:09", 0x054C, 0x03D5)
        saved_zcm2 = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:0A", 0x054C, 0x0C5E)
        saved_ds4 = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:0B", 0x054C, 0x05C4)
        saved_keyboard = write_info(bluetooth_dir, GONE_ADAPTER, "44:55:66:77:88:99", 0x05AC, 0x0255)
        saved_no_id = write_info(bluetooth_dir, GONE_ADAPTER, "55:66:77:88:99:AA", None, None)
        # A live Move's saved directory belongs to BlueZ: RemoveDevice handles it, not rmtree.
        live_dir = write_info(bluetooth_dir, HCI0, ZCM1, 0x054C, 0x03D5)
        (bluetooth_dir / GONE_ADAPTER / "settings").write_text("[General]\n")

        removed = await clear_psmove_registrations(dry_run=False, bluetooth_dir=bluetooth_dir)

        saved = sorted(r.address for r in removed if r.device_path is None)
        assert saved == ["00:06:F7:00:00:09", "00:06:F7:00:00:0A"]
        assert not saved_zcm1.exists()
        assert not saved_zcm2.exists()
        for kept in (saved_ds4, saved_keyboard, saved_no_id, live_dir):
            assert kept.exists()

    async def test_ignores_non_address_directories(self, fake_bus, bluetooth_dir):
        (bluetooth_dir / "cache").mkdir()
        write_info(bluetooth_dir, GONE_ADAPTER, "not-a-mac", 0x054C, 0x03D5)

        found = await find_psmove_registrations(bluetooth_dir=bluetooth_dir)

        assert all(r.device_path is not None for r in found)

    async def test_missing_bluetooth_dir_is_fine(self, fake_bus, tmp_path):
        found = await find_psmove_registrations(bluetooth_dir=tmp_path / "absent")

        assert len(found) == 3


class TestCli:
    async def test_main_dry_run_prints_and_removes_nothing(self, fake_bus, bluetooth_dir, capsys):
        code = await registrations.async_main(["--dry-run", "--bluetooth-dir", str(bluetooth_dir)])

        out = capsys.readouterr().out
        assert code == 0
        assert "Would remove PS Move 00:06:F7:00:00:01 from hci0" in out
        assert "Found 3 PS Move registration(s)" in out
        assert KEYBOARD not in out
        fake_bus.call.assert_not_awaited()
