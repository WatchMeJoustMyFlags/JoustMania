"""
Unit tests for HidapiAdapter — thin I/O adapter wrapping the hid (hidapi) library.

Since the `hid` module requires native hardware access, we mock it entirely
so tests can run in any environment.
"""

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

# Install a mock `hidraw` module before importing the adapter, since
# the real hidapi C extension may not be installed in test environments.
_mock_hid = ModuleType("hidraw")
_mock_hid.enumerate = MagicMock(return_value=[])
_mock_hid.device = MagicMock
sys.modules.setdefault("hidraw", _mock_hid)

from lib.controller_constants import AxisKey, ButtonKey, StateKey  # noqa: E402
from lib.psmove_hid import Button  # noqa: E402
from services.controller_manager.multiplexer.hidapi_adapter import (  # noqa: E402
    ACCEL_SCALE,
    HidapiAdapter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_PATH_1 = b"/dev/hidraw0"
FAKE_PATH_2 = b"/dev/hidraw1"
FAKE_SERIAL_RAW = "aa:bb:cc:dd:ee:ff"
FAKE_SERIAL_NORMALIZED = "AABBCCDDEEFF"
FAKE_SERIAL_2_RAW = "11:22:33:44:55:66"
FAKE_SERIAL_2_NORMALIZED = "112233445566"


def _make_dev_info(path: bytes = FAKE_PATH_1, serial: str = FAKE_SERIAL_RAW) -> dict:
    """Return a fake HID enumeration dict entry."""
    return {"path": path, "serial_number": serial}


def _make_parsed_report(
    buttons: int = 0,
    trigger: int = 0,
    accel: tuple[int, int, int] = (0, 0, 4096),
    gyro: tuple[int, int, int] = (0, 0, 0),
    battery: int = 0x05,
    temperature: int = 300,
) -> dict:
    """Return a dict matching `parse_input_report` output shape."""
    return {
        "buttons": buttons,
        "trigger": (trigger, trigger),
        "accel": (accel, accel),
        "gyro": (gyro, gyro),
        "battery": battery,
        "temperature": temperature,
        "sequence": 0,
    }


# ---------------------------------------------------------------------------
# Tests: Initialization
# ---------------------------------------------------------------------------


class TestHidapiAdapterInit:
    def test_adapter_type(self):
        adapter = HidapiAdapter()
        assert adapter.adapter_type == "hidapi"

    def test_starts_with_no_devices(self):
        adapter = HidapiAdapter()
        assert len(adapter._devices) == 0
        assert len(adapter._paths) == 0


# ---------------------------------------------------------------------------
# Tests: discover()
# ---------------------------------------------------------------------------


class TestDiscover:
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_opens_new_devices(self, mock_hid):
        """discover() should enumerate all PS Move product IDs and open new controllers."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        serials = adapter.discover()

        assert mock_hid.enumerate.call_count == 3  # ZCM1 + ZCM2 + ZCM2E
        assert FAKE_SERIAL_NORMALIZED in serials
        mock_hid.device.assert_called_once()
        mock_device.open_path.assert_called_once_with(FAKE_PATH_1)
        mock_device.set_nonblocking.assert_called_with(True)

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_normalizes_serial(self, mock_hid):
        """Serials are uppercased and colons removed."""
        mock_hid.enumerate.return_value = [_make_dev_info(serial="aA:bB:cC:01:02:03")]
        mock_hid.device.return_value = MagicMock()

        adapter = HidapiAdapter()
        serials = adapter.discover()

        assert "AABBCC010203" in serials

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_skips_empty_serial(self, mock_hid):
        """Devices with no serial_number are ignored."""
        mock_hid.enumerate.return_value = [
            {"path": FAKE_PATH_1, "serial_number": ""},
        ]

        adapter = HidapiAdapter()
        serials = adapter.discover()

        assert serials == []
        mock_hid.device.assert_not_called()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_skips_already_opened_device(self, mock_hid):
        """A device already in _devices should not be opened again."""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = MagicMock()

        adapter = HidapiAdapter()
        adapter.discover()  # First call opens the device
        mock_hid.device.reset_mock()

        adapter.discover()  # Second call should skip
        mock_hid.device.assert_not_called()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_handles_open_failure(self, mock_hid):
        """If hid.device() raises, the device is skipped."""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.side_effect = OSError("permission denied")

        adapter = HidapiAdapter()
        serials = adapter.discover()

        assert serials == []
        assert FAKE_SERIAL_NORMALIZED not in adapter._devices

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_verifies_existing_and_cleans_stale(self, mock_hid):
        """Non-force discover verifies existing devices; stale ones are cleaned."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()

        # Now make the device raise on read (simulating disconnect)
        mock_device.read.side_effect = OSError("disconnected")
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.reset_mock()
        mock_hid.device.return_value = MagicMock()

        serials = adapter.discover(force=False)

        # Stale device was cleaned, then re-opened via enumerate
        assert FAKE_SERIAL_NORMALIZED in serials

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_force_removes_disappeared_devices(self, mock_hid):
        """force=True removes devices whose paths are no longer enumerated."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()

        # Second discover with force but device path no longer in enumeration
        mock_hid.enumerate.return_value = []
        serials = adapter.discover(force=True)

        assert serials == []
        assert FAKE_SERIAL_NORMALIZED not in adapter._devices
        mock_device.close.assert_called_once()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_multiple_controllers(self, mock_hid):
        """discover() can open multiple controllers at once."""
        mock_hid.enumerate.side_effect = [
            [_make_dev_info(FAKE_PATH_1, FAKE_SERIAL_RAW)],
            [_make_dev_info(FAKE_PATH_2, FAKE_SERIAL_2_RAW)],
            [],  # ZCM2E returns nothing
        ]
        mock_hid.device.return_value = MagicMock()

        adapter = HidapiAdapter()
        serials = adapter.discover()

        assert len(serials) == 2
        assert FAKE_SERIAL_NORMALIZED in serials
        assert FAKE_SERIAL_2_NORMALIZED in serials

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_discover_no_verify_when_devices_empty(self, mock_hid):
        """When _devices is empty, _verify_existing_devices is not called (no read)."""
        mock_hid.enumerate.return_value = []

        adapter = HidapiAdapter()
        adapter.discover(force=False)

        # No device read should have been attempted
        mock_hid.device.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: open()
# ---------------------------------------------------------------------------


class TestOpen:
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_open_already_opened(self, mock_hid):
        """open() returns True immediately if the serial is already tracked."""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = MagicMock()

        adapter = HidapiAdapter()
        adapter.discover()
        mock_hid.enumerate.reset_mock()

        result = adapter.open(FAKE_SERIAL_NORMALIZED)
        assert result is True
        # Should not re-enumerate
        mock_hid.enumerate.assert_not_called()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_open_enumerates_and_opens(self, mock_hid):
        """open() re-enumerates when serial is unknown, then opens matching device."""
        adapter = HidapiAdapter()
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        result = adapter.open(FAKE_SERIAL_NORMALIZED)

        assert result is True
        assert FAKE_SERIAL_NORMALIZED in adapter._devices
        mock_hid.device.assert_called_once()
        mock_device.open_path.assert_called_once_with(FAKE_PATH_1)
        mock_device.set_nonblocking.assert_called_with(True)

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_open_normalizes_input_serial(self, mock_hid):
        """open() normalizes the input serial for comparison."""
        adapter = HidapiAdapter()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = MagicMock()

        # Pass serial with colons and lowercase — should still match
        result = adapter.open(FAKE_SERIAL_RAW)
        assert result is True

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_open_returns_false_when_not_found(self, mock_hid):
        """open() returns False if no matching serial is found on enumeration."""
        mock_hid.enumerate.return_value = []

        adapter = HidapiAdapter()
        result = adapter.open("NONEXISTENT")
        assert result is False

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_open_returns_false_on_device_error(self, mock_hid):
        """open() returns False if hid.device() raises."""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.side_effect = OSError("cannot open")

        adapter = HidapiAdapter()
        result = adapter.open(FAKE_SERIAL_NORMALIZED)
        assert result is False

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_open_skips_entries_with_empty_serial(self, mock_hid):
        """Enumerate entries with empty serial are skipped during open()."""
        mock_hid.enumerate.return_value = [
            {"path": FAKE_PATH_1, "serial_number": ""},
            _make_dev_info(FAKE_PATH_2, FAKE_SERIAL_RAW),
        ]
        mock_hid.device.return_value = MagicMock()

        adapter = HidapiAdapter()
        result = adapter.open(FAKE_SERIAL_NORMALIZED)
        assert result is True
        mock_hid.device.assert_called_once()
        mock_hid.device.return_value.open_path.assert_called_once_with(FAKE_PATH_2)


# ---------------------------------------------------------------------------
# Tests: poll()
# ---------------------------------------------------------------------------


class TestPoll:
    @patch("services.controller_manager.multiplexer.hidapi_adapter.battery_to_percent", return_value=100)
    @patch("services.controller_manager.multiplexer.hidapi_adapter.parse_input_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_poll_returns_state_dict(self, mock_hid, mock_parse, mock_battery):
        """poll() reads HID data, parses it, and returns a state dict."""
        mock_device = MagicMock()
        # Simulate one report then empty
        mock_device.read.side_effect = [b"\x00" * 49, b""]
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        parsed = _make_parsed_report(
            buttons=int(Button.MOVE | Button.CROSS),
            trigger=128,
            accel=(100, 200, 4096),
            gyro=(10, 20, 30),
            battery=0x05,
            temperature=300,
        )
        mock_parse.return_value = parsed

        adapter = HidapiAdapter()
        adapter.discover()
        state = adapter.poll(FAKE_SERIAL_NORMALIZED)

        assert state is not None
        assert state[StateKey.SERIAL] == FAKE_SERIAL_NORMALIZED
        assert state[StateKey.BATTERY] == 100
        assert state[StateKey.TRIGGER] == 128
        assert state[StateKey.TEMPERATURE] == 300
        assert state[ButtonKey.MOVE] is True
        assert state[ButtonKey.CROSS] is True
        assert state[ButtonKey.TRIANGLE] is False
        assert state[ButtonKey.PS] is False

        # Accel should be raw / ACCEL_SCALE
        assert state[StateKey.ACCEL][AxisKey.X] == 100 / ACCEL_SCALE
        assert state[StateKey.ACCEL][AxisKey.Y] == 200 / ACCEL_SCALE
        assert state[StateKey.ACCEL][AxisKey.Z] == 4096 / ACCEL_SCALE

        # Gyro should be float of raw
        assert state[StateKey.GYRO][AxisKey.X] == 10.0
        assert state[StateKey.GYRO][AxisKey.Y] == 20.0
        assert state[StateKey.GYRO][AxisKey.Z] == 30.0

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_poll_unknown_serial_returns_none(self, mock_hid):
        """poll() returns None for a serial not in _devices."""
        adapter = HidapiAdapter()
        assert adapter.poll("UNKNOWN") is None

    @patch("services.controller_manager.multiplexer.hidapi_adapter.parse_input_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_poll_drains_all_reports(self, mock_hid, mock_parse):
        """poll() reads until device.read() returns empty, keeping the last report."""
        mock_device = MagicMock()
        report_1 = b"\x01" * 49
        report_2 = b"\x02" * 49
        report_3 = b"\x03" * 49
        mock_device.read.side_effect = [report_1, report_2, report_3, b""]
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        mock_parse.return_value = _make_parsed_report()

        adapter = HidapiAdapter()
        adapter.discover()
        adapter.poll(FAKE_SERIAL_NORMALIZED)

        # parse_input_report should be called with the last non-empty report
        mock_parse.assert_called_once_with(report_3)

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_poll_returns_none_when_no_data(self, mock_hid):
        """poll() returns None if device.read() immediately returns empty."""
        mock_device = MagicMock()
        mock_device.read.return_value = b""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()
        state = adapter.poll(FAKE_SERIAL_NORMALIZED)

        assert state is None

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_poll_oserror_triggers_cleanup(self, mock_hid):
        """OSError during read triggers _cleanup, removing the device."""
        mock_device = MagicMock()
        mock_device.read.side_effect = OSError("disconnected")
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()
        assert FAKE_SERIAL_NORMALIZED in adapter._devices

        state = adapter.poll(FAKE_SERIAL_NORMALIZED)

        assert state is None
        assert FAKE_SERIAL_NORMALIZED not in adapter._devices
        assert FAKE_SERIAL_NORMALIZED not in adapter._paths
        mock_device.close.assert_called_once()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.parse_input_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_poll_non_os_error_returns_none_no_cleanup(self, mock_hid, mock_parse):
        """A non-OSError exception returns None but does NOT remove the device."""
        mock_device = MagicMock()
        mock_device.read.side_effect = [b"\x00" * 49, b""]
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        mock_parse.side_effect = ValueError("bad parse")

        adapter = HidapiAdapter()
        adapter.discover()

        state = adapter.poll(FAKE_SERIAL_NORMALIZED)

        assert state is None
        # Device should NOT have been cleaned up
        assert FAKE_SERIAL_NORMALIZED in adapter._devices

    @patch("services.controller_manager.multiplexer.hidapi_adapter.battery_to_percent", return_value=60)
    @patch("services.controller_manager.multiplexer.hidapi_adapter.parse_input_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_poll_all_buttons(self, mock_hid, mock_parse, mock_battery):
        """poll() maps all button flags correctly."""
        all_buttons = (
            Button.MOVE
            | Button.T
            | Button.PS
            | Button.CROSS
            | Button.CIRCLE
            | Button.SQUARE
            | Button.TRIANGLE
            | Button.SELECT
            | Button.START
        )
        mock_device = MagicMock()
        mock_device.read.side_effect = [b"\x00" * 49, b""]
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        mock_parse.return_value = _make_parsed_report(buttons=int(all_buttons))

        adapter = HidapiAdapter()
        adapter.discover()
        state = adapter.poll(FAKE_SERIAL_NORMALIZED)

        assert state[ButtonKey.MOVE] is True
        assert state[ButtonKey.TRIGGER] is True
        assert state[ButtonKey.PS] is True
        assert state[ButtonKey.CROSS] is True
        assert state[ButtonKey.CIRCLE] is True
        assert state[ButtonKey.SQUARE] is True
        assert state[ButtonKey.TRIANGLE] is True
        assert state[ButtonKey.SELECT] is True
        assert state[ButtonKey.START] is True

    @patch("services.controller_manager.multiplexer.hidapi_adapter.battery_to_percent", return_value=0)
    @patch("services.controller_manager.multiplexer.hidapi_adapter.parse_input_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_poll_no_buttons_pressed(self, mock_hid, mock_parse, mock_battery):
        """All buttons are False when buttons bitmask is 0."""
        mock_device = MagicMock()
        mock_device.read.side_effect = [b"\x00" * 49, b""]
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        mock_parse.return_value = _make_parsed_report(buttons=0)

        adapter = HidapiAdapter()
        adapter.discover()
        state = adapter.poll(FAKE_SERIAL_NORMALIZED)

        assert state[ButtonKey.MOVE] is False
        assert state[ButtonKey.TRIGGER] is False
        assert state[ButtonKey.PS] is False
        assert state[ButtonKey.CROSS] is False
        assert state[ButtonKey.CIRCLE] is False
        assert state[ButtonKey.SQUARE] is False
        assert state[ButtonKey.TRIANGLE] is False
        assert state[ButtonKey.SELECT] is False
        assert state[ButtonKey.START] is False


# ---------------------------------------------------------------------------
# Tests: set_output()
# ---------------------------------------------------------------------------


class TestSetOutput:
    @patch("services.controller_manager.multiplexer.hidapi_adapter.build_output_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_set_output_writes_report(self, mock_hid, mock_build):
        """set_output() builds an output report and writes it."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        fake_report = b"\x02" + b"\x00" * 48
        mock_build.return_value = fake_report

        adapter = HidapiAdapter()
        adapter.discover()

        result = adapter.set_output(FAKE_SERIAL_NORMALIZED, 255, 128, 0, 64)

        assert result is True
        mock_build.assert_called_once_with(r=255, g=128, b=0, rumble=64)
        mock_device.write.assert_called_once_with(fake_report)

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_set_output_unknown_serial_returns_false(self, mock_hid):
        """set_output() returns False for an unknown serial."""
        adapter = HidapiAdapter()
        result = adapter.set_output("UNKNOWN", 0, 0, 0, 0)
        assert result is False

    @patch("services.controller_manager.multiplexer.hidapi_adapter.build_output_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_set_output_oserror_triggers_cleanup(self, mock_hid, mock_build):
        """OSError during write triggers _cleanup."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        mock_build.return_value = b"\x00" * 49
        mock_device.write.side_effect = OSError("write failed")

        adapter = HidapiAdapter()
        adapter.discover()

        result = adapter.set_output(FAKE_SERIAL_NORMALIZED, 255, 0, 0, 0)

        assert result is False
        assert FAKE_SERIAL_NORMALIZED not in adapter._devices
        mock_device.close.assert_called_once()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.build_output_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_set_output_generic_error_returns_false_no_cleanup(self, mock_hid, mock_build):
        """A non-OSError returns False but does NOT remove the device."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        mock_build.side_effect = RuntimeError("unexpected")

        adapter = HidapiAdapter()
        adapter.discover()

        result = adapter.set_output(FAKE_SERIAL_NORMALIZED, 0, 0, 0, 0)

        assert result is False
        # Device should still be tracked (not cleaned up)
        assert FAKE_SERIAL_NORMALIZED in adapter._devices


# ---------------------------------------------------------------------------
# Tests: close() / close_all()
# ---------------------------------------------------------------------------


class TestClose:
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_close_removes_device(self, mock_hid):
        """close() removes the device from _devices and _paths, and closes handle."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()
        adapter.close(FAKE_SERIAL_NORMALIZED)

        assert FAKE_SERIAL_NORMALIZED not in adapter._devices
        assert FAKE_SERIAL_NORMALIZED not in adapter._paths
        mock_device.close.assert_called_once()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_close_nonexistent_is_safe(self, mock_hid):
        """close() on an unknown serial does not raise."""
        adapter = HidapiAdapter()
        adapter.close("NONEXISTENT")  # Should not raise

    @patch("services.controller_manager.multiplexer.hidapi_adapter.build_output_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_close_all_turns_off_leds_and_clears(self, mock_hid, mock_build):
        """close_all() sends zero LED/rumble report, closes devices, clears dicts."""
        mock_device_1 = MagicMock()
        mock_device_2 = MagicMock()
        mock_hid.enumerate.side_effect = [
            [_make_dev_info(FAKE_PATH_1, FAKE_SERIAL_RAW)],
            [_make_dev_info(FAKE_PATH_2, FAKE_SERIAL_2_RAW)],
            [],  # ZCM2E returns nothing
        ]
        mock_hid.device.side_effect = [mock_device_1, mock_device_2]
        mock_build.return_value = b"\x00" * 49

        adapter = HidapiAdapter()
        adapter.discover()
        assert len(adapter._devices) == 2

        adapter.close_all()

        assert len(adapter._devices) == 0
        assert len(adapter._paths) == 0
        mock_build.assert_called_with(r=0, g=0, b=0, rumble=0)
        mock_device_1.write.assert_called_once()
        mock_device_1.close.assert_called_once()
        mock_device_2.write.assert_called_once()
        mock_device_2.close.assert_called_once()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.build_output_report")
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_close_all_suppresses_exceptions(self, mock_hid, mock_build):
        """close_all() suppresses errors from individual device cleanup."""
        mock_device = MagicMock()
        mock_device.write.side_effect = OSError("write failed")
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        mock_build.return_value = b"\x00" * 49

        adapter = HidapiAdapter()
        adapter.discover()

        # Should not raise
        adapter.close_all()
        assert len(adapter._devices) == 0


# ---------------------------------------------------------------------------
# Tests: _cleanup()
# ---------------------------------------------------------------------------


class TestCleanup:
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_cleanup_removes_device_and_path(self, mock_hid):
        """_cleanup() removes from both _devices and _paths."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()
        adapter._cleanup(FAKE_SERIAL_NORMALIZED)

        assert FAKE_SERIAL_NORMALIZED not in adapter._devices
        assert FAKE_SERIAL_NORMALIZED not in adapter._paths
        mock_device.close.assert_called_once()

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_cleanup_suppresses_close_error(self, mock_hid):
        """_cleanup() suppresses errors from device.close()."""
        mock_device = MagicMock()
        mock_device.close.side_effect = OSError("close failed")
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()

        # Should not raise
        adapter._cleanup(FAKE_SERIAL_NORMALIZED)
        assert FAKE_SERIAL_NORMALIZED not in adapter._devices

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_cleanup_nonexistent_is_safe(self, mock_hid):
        """_cleanup() on an unknown serial does not raise."""
        adapter = HidapiAdapter()
        adapter._cleanup("NONEXISTENT")  # Should not raise


# ---------------------------------------------------------------------------
# Tests: _verify_existing_devices()
# ---------------------------------------------------------------------------


class TestVerifyExistingDevices:
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_verify_keeps_healthy_devices(self, mock_hid):
        """Devices that respond to read(0) stay in _devices."""
        mock_device = MagicMock()
        mock_device.read.return_value = b""  # Healthy non-blocking read
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()
        adapter._verify_existing_devices()

        assert FAKE_SERIAL_NORMALIZED in adapter._devices

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_verify_removes_stale_devices(self, mock_hid):
        """Devices that raise OSError on read(0) are cleaned up."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()

        # Now mark device as stale
        mock_device.read.side_effect = OSError("gone")
        adapter._verify_existing_devices()

        assert FAKE_SERIAL_NORMALIZED not in adapter._devices
        mock_device.close.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: _remove_disappeared_devices()
# ---------------------------------------------------------------------------


class TestRemoveDisappearedDevices:
    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_removes_devices_not_in_current_paths(self, mock_hid):
        """Devices whose paths are not in current_paths are removed."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()

        # Pass empty set — device path should be missing
        adapter._remove_disappeared_devices(set())
        assert FAKE_SERIAL_NORMALIZED not in adapter._devices

    @patch("services.controller_manager.multiplexer.hidapi_adapter.hid")
    def test_keeps_devices_with_current_paths(self, mock_hid):
        """Devices whose paths are in current_paths are kept."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        adapter = HidapiAdapter()
        adapter.discover()

        adapter._remove_disappeared_devices({FAKE_PATH_1})
        assert FAKE_SERIAL_NORMALIZED in adapter._devices
