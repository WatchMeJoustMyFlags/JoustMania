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

from lib.bluez_dbus import DBusError
from psmove_pairing import registrations
from psmove_pairing.registrations import (
    clear_psmove_registrations,
    find_psmove_registrations,
    is_psmove_device,
    scan_psmove_registrations,
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

    def test_non_sony_modalias_wins_over_move_like_metadata(self):
        """A non-Sony Modalias is not a Move even if name, class and HID UUID look like one."""
        props = {
            "Modalias": "usb:v05ACp0255d0100",
            "Name": "Motion Controller",
            "Class": 0x002508,
            "UUIDs": [HID_UUID],
        }
        assert not is_psmove_device(props)

    def test_navigation_controller_is_excluded(self):
        assert not is_psmove_device({"Modalias": "usb:v054Cp042Fd0100"})

    def test_empty_properties(self):
        assert not is_psmove_device({})


async def clear_all(bluetooth_dir: Path):
    return await clear_psmove_registrations(await find_psmove_registrations(bluetooth_dir))


def dbus_error_reply(name: str):
    reply = create_autospec(Message, instance=True)
    reply.message_type = MessageType.ERROR
    reply.error_name = name
    reply.body = ["boom"]
    return reply


class TestClearPsMoveRegistrations:
    async def test_removes_only_ps_move_devices(self, fake_bus, bluetooth_dir):
        """Keyboards, headsets and name-only look-alikes are never removed."""
        results = await clear_all(bluetooth_dir)

        assert sorted(r.registration.address for r in results) == [ZCM1, ZCM2, FALLBACK_MOVE]
        assert all(r.outcome == "removed" for r in results)
        assert sorted(removed_paths(fake_bus)) == [
            ("/org/bluez/hci0", "/org/bluez/hci0/dev_00_06_F7_00_00_01"),
            ("/org/bluez/hci1", "/org/bluez/hci1/dev_00_06_F7_00_00_02"),
            ("/org/bluez/hci1", "/org/bluez/hci1/dev_00_06_F7_00_00_03"),
        ]
        touched = {body for _, body in removed_paths(fake_bus)}
        for other in (KEYBOARD, HEADSET, NAME_ONLY):
            assert not any(other.replace(":", "_") in path for path in touched)

    async def test_saved_only_registrations_removed_only_for_ps_moves(self, fake_bus, bluetooth_dir):
        """Registrations of an unplugged adapter are invisible to BlueZ; only Sony Move ones are deleted."""
        saved_zcm1 = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:09", 0x054C, 0x03D5)
        saved_zcm2 = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:0A", 0x054C, 0x0C5E)
        saved_ds4 = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:0B", 0x054C, 0x05C4)
        saved_keyboard = write_info(bluetooth_dir, GONE_ADAPTER, "44:55:66:77:88:99", 0x05AC, 0x0255)
        saved_no_id = write_info(bluetooth_dir, GONE_ADAPTER, "55:66:77:88:99:AA", None, None)
        (bluetooth_dir / GONE_ADAPTER / "settings").write_text("[General]\n")

        results = await clear_all(bluetooth_dir)

        saved = sorted(r.registration.address for r in results if r.registration.device_path is None)
        assert saved == ["00:06:F7:00:00:09", "00:06:F7:00:00:0A"]
        assert not saved_zcm1.exists()
        assert not saved_zcm2.exists()
        for kept in (saved_ds4, saved_keyboard, saved_no_id):
            assert kept.exists()

    async def test_saved_dirs_of_present_adapters_are_left_to_bluez(self, fake_bus, bluetooth_dir):
        """A present adapter's storage belongs to BlueZ: even an unloaded Move there is not rmtree'd."""
        live_dir = write_info(bluetooth_dir, HCI0, ZCM1, 0x054C, 0x03D5)
        unloaded_on_present = write_info(bluetooth_dir, HCI1, "00:06:F7:00:00:0C", 0x054C, 0x03D5)

        results = await clear_all(bluetooth_dir)

        assert all(r.registration.device_path is not None for r in results)
        assert live_dir.exists()
        assert unloaded_on_present.exists()

    async def test_bluez_down_falls_back_to_saved_files(self, bluetooth_dir):
        """bluetoothd unreachable: every saved PS Move registration is handled, other devices kept."""
        saved_move = write_info(bluetooth_dir, HCI0, ZCM1, 0x054C, 0x03D5)
        saved_keyboard = write_info(bluetooth_dir, HCI0, KEYBOARD, 0x05AC, 0x0255)
        with patch.object(registrations, "get_system_bus", AsyncMock(side_effect=FileNotFoundError("no socket"))):
            scan = await scan_psmove_registrations(bluetooth_dir)
            results = await clear_psmove_registrations(scan.registrations)

        assert scan.bluez_reachable is False
        assert [r.outcome for r in results] == ["removed"]
        assert not saved_move.exists()
        assert saved_keyboard.exists()

    async def test_bluez_error_reply_also_falls_back(self, bluetooth_dir):
        with (
            patch.object(registrations, "get_system_bus", AsyncMock()),
            patch.object(
                registrations,
                "get_managed_objects",
                AsyncMock(side_effect=DBusError("org.freedesktop.DBus.Error.ServiceUnknown")),
            ),
        ):
            scan = await scan_psmove_registrations(bluetooth_dir)

        assert scan == registrations.Scan([], bluez_reachable=False)

    async def test_name_has_no_owner_also_falls_back(self, bluetooth_dir):
        saved_move = write_info(bluetooth_dir, HCI0, ZCM1, 0x054C, 0x03D5)
        with (
            patch.object(registrations, "get_system_bus", AsyncMock()),
            patch.object(
                registrations,
                "get_managed_objects",
                AsyncMock(side_effect=DBusError("org.freedesktop.DBus.Error.NameHasNoOwner")),
            ),
        ):
            scan = await scan_psmove_registrations(bluetooth_dir)

        assert scan.bluez_reachable is False
        assert [r.saved_dir for r in scan.registrations] == [saved_move]

    @pytest.mark.parametrize(
        "error_name", ["org.freedesktop.DBus.Error.AccessDenied", "org.freedesktop.DBus.Error.NoReply"]
    )
    async def test_other_dbus_errors_fail_without_touching_saved_files(self, bluetooth_dir, capsys, error_name):
        """bluetoothd may be running (busy or denied): no saved-file fallback, exit 1, nothing deleted."""
        saved_move = write_info(bluetooth_dir, HCI0, ZCM1, 0x054C, 0x03D5)
        saved_gone = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:09", 0x054C, 0x03D5)
        with (
            patch.object(registrations, "get_system_bus", AsyncMock()),
            patch.object(registrations, "get_managed_objects", AsyncMock(side_effect=DBusError(error_name))),
        ):
            with pytest.raises(DBusError):
                await scan_psmove_registrations(bluetooth_dir)
            code = await registrations.async_main(["--bluetooth-dir", str(bluetooth_dir)])

        assert code == 1
        assert error_name in capsys.readouterr().err
        assert saved_move.exists()
        assert saved_gone.exists()

    async def test_partial_failures_are_reported_per_registration(self, fake_bus, bluetooth_dir):
        """One failure does not stop the rest; DoesNotExist counts as already absent, not removed."""
        ok = fake_bus.call.return_value
        fake_bus.call.side_effect = [
            ok,
            dbus_error_reply("org.bluez.Error.DoesNotExist"),
            dbus_error_reply("org.bluez.Error.Failed"),
        ]
        seen = []

        found = await find_psmove_registrations(bluetooth_dir)
        results = await clear_psmove_registrations(found, on_result=seen.append)

        assert [r.outcome for r in results] == ["removed", "already_absent", "failed"]
        assert seen == results
        assert "org.bluez.Error.Failed" in results[2].error

    async def test_rmtree_failure_is_reported(self, fake_bus, bluetooth_dir):
        write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:09", 0x054C, 0x03D5)
        with patch.object(registrations.shutil, "rmtree", side_effect=PermissionError("read-only")):
            results = await clear_all(bluetooth_dir)

        saved = [r for r in results if r.registration.device_path is None]
        assert [(r.outcome, r.error) for r in saved] == [("failed", "read-only")]

    async def test_ignores_non_address_directories(self, fake_bus, bluetooth_dir):
        (bluetooth_dir / "cache").mkdir()
        write_info(bluetooth_dir, GONE_ADAPTER, "not-a-mac", 0x054C, 0x03D5)

        found = await find_psmove_registrations(bluetooth_dir=bluetooth_dir)

        assert all(r.device_path is not None for r in found)

    async def test_missing_bluetooth_dir_is_fine(self, fake_bus, tmp_path):
        found = await find_psmove_registrations(bluetooth_dir=tmp_path / "absent")

        assert len(found) == 3


class TestCli:
    async def test_dry_run_prints_and_removes_nothing(self, fake_bus, bluetooth_dir, capsys):
        saved = write_info(bluetooth_dir, GONE_ADAPTER, "00:06:F7:00:00:09", 0x054C, 0x03D5)

        code = await registrations.async_main(["--dry-run", "--bluetooth-dir", str(bluetooth_dir)])

        out = capsys.readouterr().out
        assert code == 0
        assert "Would remove PS Move 00:06:F7:00:00:01 from hci0" in out
        assert "Found 4 PS Move registration(s)" in out
        assert KEYBOARD not in out
        fake_bus.call.assert_not_awaited()
        assert saved.exists()

    async def test_success_exits_zero(self, fake_bus, bluetooth_dir, capsys):
        code = await registrations.async_main(["--bluetooth-dir", str(bluetooth_dir)])

        out = capsys.readouterr().out
        assert code == 0
        assert "Removed PS Move 00:06:F7:00:00:01 from hci0" in out
        assert "Removed 3, already absent 0, failed 0" in out

    async def test_any_failure_exits_non_zero_and_is_not_called_removed(self, fake_bus, bluetooth_dir, capsys):
        ok = fake_bus.call.return_value
        fake_bus.call.side_effect = [
            ok,
            dbus_error_reply("org.bluez.Error.DoesNotExist"),
            dbus_error_reply("org.bluez.Error.Failed"),
        ]

        code = await registrations.async_main(["--bluetooth-dir", str(bluetooth_dir)])

        captured = capsys.readouterr()
        assert code == 1
        assert "Already absent: PS Move" in captured.out
        assert "FAILED to remove PS Move" in captured.err
        assert "Removed 1, already absent 1, failed 1" in captured.out


# --- One controller on every adapter: unpair (UC-005) and re-pair cleanup (UC-001 BR-004) ---

CONTROLLER = "00:06:F7:AA:BB:CC"
OTHER_CONTROLLER = "00:06:F7:11:22:33"
ZCM1_MODALIAS = "usb:v054Cp03D5d0001"


def adapters_tree(adapters: dict[str, tuple[str, dict[str, dict]]]) -> dict:
    """{"hci0": (adapter_address, {"dev_x": device(...)})} -> GetManagedObjects reply."""
    objects: dict = {"/org/bluez": {"org.bluez.AgentManager1": {}}}
    for hci, (address, devices) in adapters.items():
        objects[f"/org/bluez/{hci}"] = {"org.bluez.Adapter1": {"Address": Variant("s", address)}}
        for name, interfaces in devices.items():
            objects[f"/org/bluez/{hci}/{name}"] = interfaces
    return objects


@pytest.fixture
def bluez():
    """Patch the D-Bus edge; yields (bus, set_objects). set_objects(exception) makes the tree call raise."""
    bus = create_autospec(MessageBus, instance=True)
    bus.call.return_value = Message(message_type=MessageType.METHOD_RETURN, reply_serial=1)
    state: dict = {"objects": adapters_tree({})}

    async def managed_objects(_bus):
        if isinstance(state["objects"], BaseException):
            raise state["objects"]
        return state["objects"]

    with (
        patch.object(registrations, "get_system_bus", AsyncMock(return_value=bus)),
        patch.object(registrations, "get_managed_objects", side_effect=managed_objects),
    ):
        yield bus, lambda objects: state.update(objects=objects)


def outcomes(results) -> list[tuple[str, str]]:
    return [(r.registration.adapter_address, r.outcome) for r in results]


class TestNormalizeAddress:
    @pytest.mark.uc("UC-005", flow="A5")
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("00:06:f7:aa:bb:cc", CONTROLLER),
            (" 00-06-F7-AA-BB-CC ", CONTROLLER),
            ("MOCK0001", None),
            ("00:06:F7:AA:BB", None),
            ("../../etc", None),
        ],
    )
    def test_normalize(self, raw, expected):
        assert registrations.normalize_address(raw) == expected


class TestRemoveControllerRegistrations:
    @pytest.mark.uc("UC-005", flow="main", steps=[5], br=["BR-001"])
    async def test_removes_that_controller_on_every_adapter_only(self, bluez, bluetooth_dir):
        """Both adapters' registrations of the controller go; other controllers and devices stay."""
        bus, set_objects = bluez
        set_objects(
            adapters_tree(
                {
                    "hci0": (
                        HCI0,
                        {
                            "dev_a": device(CONTROLLER, Modalias=ZCM1_MODALIAS),
                            "dev_b": device(OTHER_CONTROLLER, Modalias=ZCM1_MODALIAS),
                        },
                    ),
                    "hci1": (
                        HCI1,
                        {
                            "dev_a": device(CONTROLLER, Name="Motion Controller", Class=0x002508, UUIDs=[HID_UUID]),
                            "dev_k": device(KEYBOARD, Modalias="usb:v05ACp0255d0100"),
                        },
                    ),
                }
            )
        )

        results = await registrations.remove_controller_registrations(CONTROLLER.lower(), bluetooth_dir=bluetooth_dir)

        assert outcomes(results) == [(HCI0, "removed"), (HCI1, "removed")]
        assert removed_paths(bus) == [
            ("/org/bluez/hci0", "/org/bluez/hci0/dev_a"),
            ("/org/bluez/hci1", "/org/bluez/hci1/dev_a"),
        ]

    @pytest.mark.uc("UC-005", flow="main", steps=[5], br=["BR-001"])
    async def test_property_less_copies_go_once_a_ps_move_matched(self, bluez, bluetooth_dir):
        """FR-123 'every adapter': a never-connected copy (no Modalias/class) goes with the matched PS Move."""
        bus, set_objects = bluez
        set_objects(
            adapters_tree(
                {
                    "hci0": (HCI0, {"dev_a": device(CONTROLLER, Modalias=ZCM1_MODALIAS)}),
                    "hci1": (HCI1, {"dev_a": device(CONTROLLER)}),  # trusted over USB, never connected
                }
            )
        )
        saved_bare = write_info(bluetooth_dir, GONE_ADAPTER, CONTROLLER, None, None)

        results = await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir)

        assert outcomes(results) == [(HCI0, "removed"), (HCI1, "removed"), (GONE_ADAPTER, "removed")]
        assert len(removed_paths(bus)) == 2
        assert not saved_bare.exists()

    @pytest.mark.uc("UC-005", flow="A4", br=["BR-001"])
    async def test_only_property_less_copies_are_not_enough(self, bluez, bluetooth_dir):
        """Without any registration identified as a PS Move, nothing is taken (A4)."""
        bus, set_objects = bluez
        set_objects(adapters_tree({"hci0": (HCI0, {"dev_a": device(CONTROLLER)})}))

        assert await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir) == []
        assert removed_paths(bus) == []

    @pytest.mark.uc("UC-005", br=["BR-001"])
    async def test_non_psmove_device_with_that_address_is_untouched(self, bluez, bluetooth_dir):
        """BR-001: a copy identified as another device never goes, even when a PS Move copy matched."""
        bus, set_objects = bluez
        set_objects(
            adapters_tree(
                {
                    "hci0": (HCI0, {"dev_k": device(CONTROLLER, Modalias="usb:v05ACp0255d0100")}),
                    "hci1": (HCI1, {"dev_a": device(CONTROLLER, Modalias=ZCM1_MODALIAS)}),
                }
            )
        )

        results = await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir)

        assert outcomes(results) == [(HCI1, "removed")]
        assert removed_paths(bus) == [("/org/bluez/hci1", "/org/bluez/hci1/dev_a")]

    @pytest.mark.uc("UC-005", flow="main", steps=[5], br=["BR-002"])
    async def test_saved_only_registration_of_absent_adapter_is_deleted(self, bluez, bluetooth_dir):
        """BR-002: absent adapter's PS Move dir deleted; a present adapter's storage is left to BlueZ."""
        bus, set_objects = bluez
        set_objects(adapters_tree({"hci0": (HCI0, {})}))
        absent = write_info(bluetooth_dir, GONE_ADAPTER, CONTROLLER, 0x054C, 0x03D5)
        present = write_info(bluetooth_dir, HCI0, CONTROLLER, 0x054C, 0x03D5)
        other = write_info(bluetooth_dir, GONE_ADAPTER, OTHER_CONTROLLER, 0x054C, 0x03D5)
        not_move = write_info(bluetooth_dir, "DD:00:00:00:00:00", CONTROLLER, 0x05AC, 0x0255)

        results = await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir)

        assert outcomes(results) == [(GONE_ADAPTER, "removed")]
        assert not absent.exists()
        assert present.exists() and other.exists() and not_move.exists()
        assert removed_paths(bus) == []

    @pytest.mark.uc("UC-005", flow="A7", br=["BR-002"])
    async def test_bluetoothd_not_running_falls_back_to_saved_files(self, bluez, bluetooth_dir):
        """Nobody owns org.bluez: every saved PS Move dir of the address may go (bluetoothd reads them at start)."""
        bus, set_objects = bluez
        set_objects(DBusError("org.freedesktop.DBus.Error.ServiceUnknown"))
        saved = write_info(bluetooth_dir, HCI0, CONTROLLER, 0x054C, 0x03D5)

        results = await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir)

        assert outcomes(results) == [(HCI0, "removed")]
        assert not saved.exists()
        bus.call.assert_not_awaited()

    @pytest.mark.uc("UC-005", flow="A7")
    async def test_other_dbus_error_raises_and_touches_nothing(self, bluez, bluetooth_dir):
        """bluetoothd may be running (AccessDenied, NoReply): its storage is not touched."""
        _bus, set_objects = bluez
        set_objects(DBusError("org.freedesktop.DBus.Error.NoReply"))
        saved = write_info(bluetooth_dir, HCI0, CONTROLLER, 0x054C, 0x03D5)

        with pytest.raises(DBusError):
            await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir)
        assert saved.exists()

    @pytest.mark.uc("UC-005", flow="A4")
    async def test_unknown_address_finds_nothing(self, bluez, bluetooth_dir):
        bus, set_objects = bluez
        set_objects(adapters_tree({"hci0": (HCI0, {"dev_b": device(OTHER_CONTROLLER, Modalias=ZCM1_MODALIAS)})}))

        assert await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir) == []
        assert removed_paths(bus) == []

    @pytest.mark.uc("UC-005", flow="A4", br=["BR-002"])
    async def test_already_gone_counts_as_already_absent(self, bluez, bluetooth_dir):
        """A race with BlueZ: DoesNotExist is 'already_absent', which the API treats as success."""
        bus, set_objects = bluez
        set_objects(adapters_tree({"hci0": (HCI0, {"dev_a": device(CONTROLLER, Modalias=ZCM1_MODALIAS)})}))
        bus.call.return_value = dbus_error_reply("org.bluez.Error.DoesNotExist")

        results = await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir)

        assert outcomes(results) == [(HCI0, "already_absent")]

    @pytest.mark.uc("UC-005", flow="A5")
    async def test_invalid_address_is_rejected_before_bluez(self, bluez, bluetooth_dir):
        bus, _ = bluez
        with pytest.raises(ValueError, match="not a Bluetooth address"):
            await registrations.remove_controller_registrations("MOCK0001", bluetooth_dir=bluetooth_dir)
        bus.call.assert_not_awaited()

    @pytest.mark.uc("UC-005", flow="A7")
    async def test_removal_failure_is_reported_per_registration(self, bluez, bluetooth_dir):
        bus, set_objects = bluez
        set_objects(adapters_tree({"hci0": (HCI0, {"dev_a": device(CONTROLLER, Modalias=ZCM1_MODALIAS)})}))
        bus.call.return_value = dbus_error_reply("org.bluez.Error.Failed")

        results = await registrations.remove_controller_registrations(CONTROLLER, bluetooth_dir=bluetooth_dir)

        assert outcomes(results) == [(HCI0, "failed")]
        assert "org.bluez.Error.Failed" in results[0].error

    @pytest.mark.uc("UC-001", flow="A5", br=["BR-004"])
    async def test_repair_cleanup_keeps_selected_adapter(self, bluez, bluetooth_dir):
        """BR-004: other adapters' registrations go (by address alone, even never-connected ones); the target stays."""
        bus, set_objects = bluez
        set_objects(
            adapters_tree(
                {
                    "hci0": (HCI0, {"dev_a": device(CONTROLLER)}),  # never connected: no name, class or modalias
                    "hci1": (HCI1, {"dev_a": device(CONTROLLER, Modalias=ZCM1_MODALIAS)}),
                    "hci2": ("EE:00:00:00:00:00", {"dev_k": device(KEYBOARD)}),
                }
            )
        )
        absent = write_info(bluetooth_dir, GONE_ADAPTER, CONTROLLER, None, None)  # saved, no DeviceID
        loaded_dir = write_info(bluetooth_dir, HCI0, CONTROLLER, None, None)  # BlueZ-owned: removed via D-Bus only

        results = await registrations.remove_controller_registrations(
            CONTROLLER, keep_adapter=HCI1.lower(), require_psmove=False, bluetooth_dir=bluetooth_dir
        )

        assert outcomes(results) == [(HCI0, "removed"), (GONE_ADAPTER, "removed")]
        assert removed_paths(bus) == [("/org/bluez/hci0", "/org/bluez/hci0/dev_a")]
        assert not absent.exists()
        assert loaded_dir.exists()  # the fake bus does not delete it; real BlueZ does on RemoveDevice
