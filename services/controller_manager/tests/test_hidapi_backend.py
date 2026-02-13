"""
Unit tests for HidapiBackend.

Tests the pure-Python hidapi-based PS Move controller backend.
All hardware access is mocked via unittest.mock.

Issue: feat/hidapi-backend
"""

import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))

from lib.controller_constants import (
    AxisKey,
    ButtonKey,
    StateKey,
)
from lib.psmove_hid import (
    INPUT_REPORT_SIZE,
    Battery,
    Button,
)


def _build_hid_report(
    buttons_lo=0,
    buttons_hi=0,
    trigger2=0,
    battery=Battery.MAX,
    ax2=0x8000,
    ay2=0x8000,
    az2=0x9000,  # ~1g on z
    gx2=0x8000,
    gy2=0x8000,
    gz2=0x8000,
    temperature=300,
) -> bytes:
    """Build a synthetic HID input report for testing."""
    data = bytearray(INPUT_REPORT_SIZE)
    data[1] = buttons_lo & 0xFF
    data[2] = (buttons_lo >> 8) & 0xFF
    data[3] = buttons_hi & 0xFF
    data[7] = trigger2
    data[12] = battery
    struct.pack_into("<HHH", data, 19, ax2, ay2, az2)
    struct.pack_into("<HHH", data, 31, gx2, gy2, gz2)
    struct.pack_into("<H", data, 37, temperature)
    return bytes(data)


class FakeHidDevice:
    """Mock hid.Device for testing."""

    def __init__(self, path=None):
        self.path = path
        self.nonblocking = False
        self._reports = []  # Queue of reports to return from read()
        self._written = []  # Capture write() calls
        self._closed = False

    def read(self, size):
        if self._reports:
            return self._reports.pop(0)
        return b""

    def write(self, data):
        self._written.append(data)
        return len(data)

    def close(self):
        self._closed = True

    def queue_report(self, report: bytes):
        """Queue a report to be returned by the next read() call."""
        self._reports.append(report)


@pytest.fixture
def mock_hid():
    """Mock the hid module."""
    mock = MagicMock()
    mock.enumerate.return_value = []
    mock.Device = FakeHidDevice
    return mock


@pytest.fixture
def backend(mock_hid):
    """Create a HidapiBackend with mocked hid module."""
    with patch.dict(sys.modules, {"hid": mock_hid}):
        # Re-import to pick up the mock
        import importlib

        import services.controller_manager.hidapi_backend as backend_mod

        importlib.reload(backend_mod)
        return backend_mod.HidapiBackend()


class TestInitialize:
    """Test backend initialization."""

    @pytest.mark.asyncio
    async def test_initialize_success(self, backend):
        result = await backend.initialize()
        assert result is True
        assert backend.running is True

    @pytest.mark.asyncio
    async def test_initialize_opens_found_controllers(self, mock_hid, backend):
        """Controllers found during init should be opened."""
        device = FakeHidDevice(path=b"/dev/hidraw0")
        mock_hid.enumerate.return_value = [{"path": b"/dev/hidraw0", "serial_number": "AA:BB:CC:DD:EE:FF"}]
        mock_hid.Device = lambda path=None: device  # noqa: ARG005

        result = await backend.initialize()
        assert result is True
        assert "AABBCCDDEEFF" in backend.controllers


class TestGetControllerState:
    """Test reading controller state from HID reports."""

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_serial(self, backend):
        result = await backend.get_controller_state("UNKNOWN")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_data(self, backend):
        """Non-blocking read with no data should return None."""
        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device
        result = await backend.get_controller_state("TESTSERIAL")
        assert result is None

    @pytest.mark.asyncio
    async def test_parses_report_correctly(self, backend):
        """A valid HID report should produce correct state dict."""
        device = FakeHidDevice()
        report = _build_hid_report(
            buttons_lo=Button.TRIANGLE,
            trigger2=128,
            battery=Battery.P80,
            az2=0x9000,  # +4096 raw = ~1g
        )
        device.queue_report(report)
        backend.controllers["TESTSERIAL"] = device

        state = await backend.get_controller_state("TESTSERIAL")
        assert state is not None
        assert state[StateKey.SERIAL] == "TESTSERIAL"
        assert state[ButtonKey.TRIANGLE] is True
        assert state[ButtonKey.CIRCLE] is False
        assert state[StateKey.TRIGGER] == 128
        assert state[StateKey.BATTERY] == 80  # P80 -> 80%
        # Z-axis: (0x9000 - 0x8000) / 4096 = 1.0
        assert abs(state[StateKey.ACCEL][AxisKey.Z] - 1.0) < 0.01

    @pytest.mark.asyncio
    async def test_reads_latest_report(self, backend):
        """When multiple reports are queued, should use the latest."""
        device = FakeHidDevice()
        device.queue_report(_build_hid_report(trigger2=50))
        device.queue_report(_build_hid_report(trigger2=200))
        backend.controllers["TESTSERIAL"] = device

        state = await backend.get_controller_state("TESTSERIAL")
        assert state[StateKey.TRIGGER] == 200

    @pytest.mark.asyncio
    async def test_button_mapping(self, backend):
        """All buttons should be correctly mapped."""
        device = FakeHidDevice()
        lo = Button.CROSS | Button.CIRCLE | Button.SELECT | Button.START
        hi = (Button.MOVE | Button.T | Button.PS) >> 16
        device.queue_report(_build_hid_report(buttons_lo=lo, buttons_hi=hi))
        backend.controllers["TESTSERIAL"] = device

        state = await backend.get_controller_state("TESTSERIAL")
        assert state[ButtonKey.CROSS] is True
        assert state[ButtonKey.CIRCLE] is True
        assert state[ButtonKey.SELECT] is True
        assert state[ButtonKey.START] is True
        assert state[ButtonKey.MOVE] is True
        assert state[ButtonKey.TRIGGER] is True  # T button
        assert state[ButtonKey.PS] is True
        assert state[ButtonKey.TRIANGLE] is False
        assert state[ButtonKey.SQUARE] is False

    @pytest.mark.asyncio
    async def test_io_error_cleans_up(self, backend):
        """OSError during read should clean up the controller."""
        device = MagicMock()
        device.read.side_effect = OSError("Device disconnected")
        backend.controllers["TESTSERIAL"] = device
        backend._device_paths["TESTSERIAL"] = b"/dev/hidraw0"

        state = await backend.get_controller_state("TESTSERIAL")
        assert state is None
        assert "TESTSERIAL" not in backend.controllers


class TestSetLedColor:
    """Test LED color setting."""

    @pytest.mark.asyncio
    async def test_set_led_writes_report(self, backend):
        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device

        result = await backend.set_led_color("TESTSERIAL", 255, 128, 0)
        assert result is True
        assert len(device._written) == 1
        report = device._written[0]
        assert report[2] == 255  # R
        assert report[3] == 128  # G
        assert report[4] == 0  # B

    @pytest.mark.asyncio
    async def test_set_led_tracks_color(self, backend):
        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device

        await backend.set_led_color("TESTSERIAL", 100, 200, 50)
        assert backend.led_colors["TESTSERIAL"] == (100, 200, 50)
        assert backend._last_sent_color["TESTSERIAL"] == (100, 200, 50)

    @pytest.mark.asyncio
    async def test_set_led_unknown_serial(self, backend):
        result = await backend.set_led_color("UNKNOWN", 255, 0, 0)
        assert result is False


class TestSetRumble:
    """Test rumble setting."""

    @pytest.mark.asyncio
    async def test_set_rumble_writes_report(self, backend):
        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device
        backend.led_colors["TESTSERIAL"] = (255, 0, 0)

        result = await backend.set_rumble("TESTSERIAL", 128)
        assert result is True
        report = device._written[0]
        assert report[6] == 128  # Rumble
        assert report[2] == 255  # Preserves LED R

    @pytest.mark.asyncio
    async def test_set_rumble_unknown_serial(self, backend):
        result = await backend.set_rumble("UNKNOWN", 128)
        assert result is False


class TestEffectActive:
    """Test effect active flag."""

    def test_set_effect_active(self, backend):
        backend.set_effect_active("TESTSERIAL", True)
        assert "TESTSERIAL" in backend._effect_active

    def test_clear_effect_active(self, backend):
        backend._effect_active.add("TESTSERIAL")
        backend.set_effect_active("TESTSERIAL", False)
        assert "TESTSERIAL" not in backend._effect_active

    def test_clear_nonexistent(self, backend):
        backend.set_effect_active("NONEXISTENT", False)
        assert "NONEXISTENT" not in backend._effect_active


class TestUpdateAllLeds:
    """Test batch LED update logic."""

    def test_skips_effect_active(self, backend):
        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device
        backend.led_colors["TESTSERIAL"] = (255, 0, 0)
        backend._effect_active.add("TESTSERIAL")

        updated = backend.update_all_leds()
        assert updated == 0
        assert len(device._written) == 0

    def test_updates_changed_color(self, backend):
        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device
        backend.led_colors["TESTSERIAL"] = (255, 0, 0)
        backend._last_sent_color["TESTSERIAL"] = (0, 0, 0)  # Different
        backend._last_led_update["TESTSERIAL"] = 0

        updated = backend.update_all_leds()
        assert updated == 1
        assert len(device._written) == 1

    def test_keepalive_after_4_seconds(self, backend):
        import time

        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device
        backend.led_colors["TESTSERIAL"] = (255, 0, 0)
        backend._last_sent_color["TESTSERIAL"] = (255, 0, 0)  # Same color
        backend._last_led_update["TESTSERIAL"] = time.time() - 5.0  # 5s ago

        updated = backend.update_all_leds()
        assert updated == 1

    def test_no_update_when_recent(self, backend):
        import time

        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device
        backend.led_colors["TESTSERIAL"] = (255, 0, 0)
        backend._last_sent_color["TESTSERIAL"] = (255, 0, 0)
        backend._last_led_update["TESTSERIAL"] = time.time()  # Just now

        updated = backend.update_all_leds()
        assert updated == 0


class TestGetConnectedControllers:
    """Test controller enumeration."""

    def test_returns_tracked_controllers(self, backend):
        backend.controllers["SERIAL1"] = FakeHidDevice()
        backend.controllers["SERIAL2"] = FakeHidDevice()
        result = backend.get_connected_controllers()
        assert set(result) == {"SERIAL1", "SERIAL2"}

    def test_force_rescan_finds_new_controllers(self, backend, mock_hid):
        """Force rescan should enumerate and open new controllers."""
        device = FakeHidDevice(path=b"/dev/hidraw1")
        mock_hid.enumerate.return_value = [{"path": b"/dev/hidraw1", "serial_number": "11:22:33:44:55:66"}]
        mock_hid.Device = lambda path=None: device  # noqa: ARG005

        result = backend.get_connected_controllers(force_rescan=True)
        assert "112233445566" in result


class TestDisconnect:
    """Test controller disconnection."""

    @pytest.mark.asyncio
    async def test_disconnect_closes_device(self, backend):
        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device
        backend._device_paths["TESTSERIAL"] = b"/dev/hidraw0"
        backend.led_colors["TESTSERIAL"] = (255, 0, 0)

        result = await backend.disconnect_controller("TESTSERIAL")
        assert result is True
        assert device._closed is True
        assert "TESTSERIAL" not in backend.controllers
        assert "TESTSERIAL" not in backend._device_paths
        assert "TESTSERIAL" not in backend.led_colors

    @pytest.mark.asyncio
    async def test_disconnect_unknown_serial(self, backend):
        result = await backend.disconnect_controller("UNKNOWN")
        assert result is True  # No-op is still success


class TestShutdown:
    """Test backend shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_closes_all_devices(self, backend):
        d1 = FakeHidDevice()
        d2 = FakeHidDevice()
        backend.controllers["S1"] = d1
        backend.controllers["S2"] = d2

        await backend.shutdown()
        assert d1._closed is True
        assert d2._closed is True
        assert len(backend.controllers) == 0
        assert backend.running is False

    @pytest.mark.asyncio
    async def test_shutdown_turns_off_leds(self, backend):
        device = FakeHidDevice()
        backend.controllers["TESTSERIAL"] = device

        await backend.shutdown()
        # Should have written a report with LEDs off
        assert len(device._written) == 1
        report = device._written[0]
        assert report[2] == 0  # R
        assert report[3] == 0  # G
        assert report[4] == 0  # B
        assert report[6] == 0  # Rumble


class TestScanControllers:
    """Test controller scanning."""

    @pytest.mark.asyncio
    async def test_scan_returns_found_controllers(self, backend, mock_hid):
        mock_hid.enumerate.return_value = [
            {"path": b"/dev/hidraw0", "serial_number": "AA:BB:CC:DD:EE:FF"},
            {"path": b"/dev/hidraw1", "serial_number": "11:22:33:44:55:66"},
        ]

        controllers = await backend.scan_controllers()
        assert len(controllers) == 2
        serials = {c["serial"] for c in controllers}
        assert "AABBCCDDEEFF" in serials
        assert "112233445566" in serials

    @pytest.mark.asyncio
    async def test_scan_deduplicates(self, backend, mock_hid):
        """Same serial on different interfaces should appear once."""
        mock_hid.enumerate.return_value = [
            {"path": b"/dev/hidraw0", "serial_number": "AA:BB:CC:DD:EE:FF"},
            {"path": b"/dev/hidraw1", "serial_number": "AA:BB:CC:DD:EE:FF"},
        ]

        controllers = await backend.scan_controllers()
        assert len(controllers) == 1

    @pytest.mark.asyncio
    async def test_scan_skips_no_serial(self, backend, mock_hid):
        mock_hid.enumerate.return_value = [
            {"path": b"/dev/hidraw0", "serial_number": ""},
        ]

        controllers = await backend.scan_controllers()
        assert len(controllers) == 0


class TestConnectController:
    """Test controller connection."""

    @pytest.mark.asyncio
    async def test_connect_already_connected(self, backend):
        backend.controllers["TESTSERIAL"] = FakeHidDevice()
        result = await backend.connect_controller("TESTSERIAL")
        assert result is True

    @pytest.mark.asyncio
    async def test_connect_not_found(self, backend, mock_hid):
        mock_hid.enumerate.return_value = []
        result = await backend.connect_controller("UNKNOWN")
        assert result is False


class TestGetRssi:
    """Test RSSI reading capability."""

    def test_rssi_disabled_by_default(self, backend):
        result = backend.get_rssi("AABBCCDDEEFF")
        assert result is None

    def test_rssi_serial_to_mac_conversion(self, backend):
        """Test MAC address formatting from serial."""
        backend._rssi_reader = MagicMock()
        backend._rssi_reader.get_rssi.return_value = -50

        result = backend.get_rssi("AABBCCDDEEFF")
        assert result == -50
        backend._rssi_reader.get_rssi.assert_called_once_with("AA:BB:CC:DD:EE:FF")
