"""
Unit tests for DeviceManager — command handling and device operations.

The `hid` module requires native hardware access, so we mock it entirely.
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

# Ensure mock hidraw module exists
_mock_hid = ModuleType("hidraw")
_mock_hid.enumerate = MagicMock(return_value=[])
_mock_hid.device = MagicMock
sys.modules.setdefault("hidraw", _mock_hid)

from lib.psmove_hid import PRODUCT_ID_ZCM1, Button  # noqa: E402
from services.python_hid.device_manager import (  # noqa: E402
    ACCEL_SCALE,
    Command,
    CommandType,
    DeviceManager,
    SensorData,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_PATH_1 = b"/dev/hidraw0"
FAKE_SERIAL_RAW = "aa:bb:cc:dd:ee:ff"
FAKE_SERIAL_NORMALIZED = "AABBCCDDEEFF"


def _make_dev_info(
    path: bytes = FAKE_PATH_1,
    serial: str = FAKE_SERIAL_RAW,
    product_id: int = PRODUCT_ID_ZCM1,
) -> dict:
    return {"path": path, "serial_number": serial, "product_id": product_id}


def _make_parsed_report(
    buttons: int = 0,
    trigger: int = 0,
    accel: tuple[int, int, int] = (0, 0, 4096),
    gyro: tuple[int, int, int] = (0, 0, 0),
    battery: int = 0x05,
    temperature: int = 300,
) -> dict:
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
# Tests: Command handling
# ---------------------------------------------------------------------------


class TestCommandDispatch:
    """Test that commands are dispatched correctly."""

    @patch("services.python_hid.device_manager.hid")
    def test_discover_returns_device_list(self, mock_hid):
        """Discover command returns list of device info dicts."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.DISCOVER, args={"force": False, "verify_only": False}))

        assert len(result) == 1
        assert result[0]["serial"] == FAKE_SERIAL_NORMALIZED

    @patch("services.python_hid.device_manager.hid")
    def test_open_returns_true_on_success(self, mock_hid):
        """Open command returns True when device is found."""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = MagicMock()

        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.OPEN, args={"serial": FAKE_SERIAL_NORMALIZED}))
        assert result is True

    @patch("services.python_hid.device_manager.hid")
    def test_open_returns_false_when_not_found(self, mock_hid):
        """Open command returns False when device is not found."""
        mock_hid.enumerate.return_value = []

        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.OPEN, args={"serial": "NONEXISTENT"}))
        assert result is False

    @patch("services.python_hid.device_manager.hid")
    def test_close_returns_true(self, mock_hid):
        """Close command always returns True."""
        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.CLOSE, args={"serial": "NONEXISTENT"}))
        assert result is True

    @patch("services.python_hid.device_manager.hid")
    def test_close_all_returns_true(self, mock_hid):
        """CloseAll command returns True."""
        dm = DeviceManager()
        result = dm._handle_command(Command(type=CommandType.CLOSE_ALL))
        assert result is True

    @patch("services.python_hid.device_manager.build_output_report")
    @patch("services.python_hid.device_manager.hid")
    def test_set_output_returns_true(self, mock_hid, mock_build):
        """SetOutput returns True when device exists."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device
        mock_build.return_value = b"\x00" * 9

        dm = DeviceManager()
        dm._handle_command(Command(type=CommandType.DISCOVER, args={"force": False, "verify_only": False}))

        result = dm._handle_command(
            Command(
                type=CommandType.SET_OUTPUT,
                args={"serial": FAKE_SERIAL_NORMALIZED, "r": 255, "g": 0, "b": 0, "rumble": 0},
            )
        )
        assert result is True

    @patch("services.python_hid.device_manager.hid")
    def test_set_output_returns_false_unknown_serial(self, mock_hid):
        """SetOutput returns False for unknown serial."""
        dm = DeviceManager()
        result = dm._handle_command(
            Command(
                type=CommandType.SET_OUTPUT,
                args={"serial": "UNKNOWN", "r": 0, "g": 0, "b": 0, "rumble": 0},
            )
        )
        assert result is False


# ---------------------------------------------------------------------------
# Tests: Device reading / SensorData
# ---------------------------------------------------------------------------


class TestReadDevice:
    @patch("services.python_hid.device_manager.battery_to_percent", return_value=100)
    @patch("services.python_hid.device_manager.parse_input_report")
    @patch("services.python_hid.device_manager.hid")
    def test_read_device_produces_sensor_data(self, mock_hid, mock_parse, mock_battery):
        """_read_device returns SensorData with correct fields."""
        mock_device = MagicMock()
        mock_device.read.side_effect = [b"\x00" * 49, b""]

        mock_parse.return_value = _make_parsed_report(
            buttons=int(Button.MOVE | Button.CROSS),
            trigger=128,
            accel=(100, 200, 4096),
            gyro=(10, 20, 30),
            battery=0x05,
            temperature=300,
        )

        dm = DeviceManager()
        dm._product_ids[FAKE_SERIAL_NORMALIZED] = PRODUCT_ID_ZCM1
        result = dm._read_device(FAKE_SERIAL_NORMALIZED, mock_device)

        assert isinstance(result, SensorData)
        assert result.serial == FAKE_SERIAL_NORMALIZED
        assert result.battery == 100
        assert result.trigger == 128
        assert result.temperature == 300
        assert result.button_move is True
        assert result.button_cross is True
        assert result.button_triangle is False
        assert result.accel_x == 100 / ACCEL_SCALE
        assert result.accel_z == 4096 / ACCEL_SCALE
        assert result.gyro_x == 10.0

    @patch("services.python_hid.device_manager.hid")
    def test_read_device_returns_none_when_no_data(self, mock_hid):
        """_read_device returns None when device has no new data."""
        mock_device = MagicMock()
        mock_device.read.return_value = b""

        dm = DeviceManager()
        result = dm._read_device(FAKE_SERIAL_NORMALIZED, mock_device)
        assert result is None

    @patch("services.python_hid.device_manager.hid")
    def test_read_device_returns_none_on_oserror(self, mock_hid):
        """_read_device returns None and cleans up on OSError."""
        mock_device = MagicMock()
        mock_device.read.side_effect = OSError("disconnected")

        dm = DeviceManager()
        dm._devices[FAKE_SERIAL_NORMALIZED] = mock_device
        dm._paths[FAKE_SERIAL_NORMALIZED] = "/dev/hidraw0"

        result = dm._read_device(FAKE_SERIAL_NORMALIZED, mock_device)
        assert result is None
        assert FAKE_SERIAL_NORMALIZED not in dm._devices


# ---------------------------------------------------------------------------
# Tests: Stream registration
# ---------------------------------------------------------------------------


class TestStreamRegistration:
    def test_register_and_unregister_stream(self):
        """Streams can be registered and unregistered."""
        dm = DeviceManager()
        q = dm.register_stream("test-stream")
        assert q is not None
        assert "test-stream" in dm._stream_queues

        dm.unregister_stream("test-stream")
        assert "test-stream" not in dm._stream_queues

    def test_unregister_nonexistent_is_safe(self):
        """Unregistering a non-existent stream does not raise."""
        dm = DeviceManager()
        dm.unregister_stream("nonexistent")


# ---------------------------------------------------------------------------
# Tests: Discover edge cases
# ---------------------------------------------------------------------------


class TestDiscoverEdgeCases:
    @patch("services.python_hid.device_manager.hid")
    def test_discover_skips_empty_serial(self, mock_hid):
        """Devices with empty serial are skipped."""
        mock_hid.enumerate.return_value = [
            {"path": FAKE_PATH_1, "serial_number": ""},
        ]

        dm = DeviceManager()
        result = dm._discover()
        assert result == []

    @patch("services.python_hid.device_manager.hid")
    def test_discover_excludes_serials(self, mock_hid):
        """Excluded serials are not opened."""
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = MagicMock()

        dm = DeviceManager()
        result = dm._discover(exclude_serials=[FAKE_SERIAL_NORMALIZED])
        assert result == []

    @patch("services.python_hid.device_manager.hid")
    def test_discover_verify_cleans_stale(self, mock_hid):
        """Stale devices are cleaned during verify."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        dm = DeviceManager()
        dm._discover()

        mock_device.read.side_effect = OSError("gone")
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.reset_mock()
        mock_hid.device.return_value = MagicMock()

        result = dm._discover(force=False)
        assert len(result) == 1

    @patch("services.python_hid.device_manager.hid")
    def test_discover_force_removes_disappeared(self, mock_hid):
        """force=True removes devices whose paths are gone."""
        mock_device = MagicMock()
        mock_hid.enumerate.return_value = [_make_dev_info()]
        mock_hid.device.return_value = mock_device

        dm = DeviceManager()
        dm._discover()

        mock_hid.enumerate.return_value = []
        result = dm._discover(force=True)
        assert result == []
        mock_device.close.assert_called_once()
