"""Tests for psmove_pairing.usb_pairing module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from psmove_pairing.adapter_manager import AdapterInfo
from psmove_pairing.usb_pairing import USBPairing

from .conftest import MockCommandRunner

# Fake HID device paths (bytes, as returned by hidapi)
FAKE_PATH_1 = b"/dev/hidraw3"
FAKE_PATH_2 = b"/dev/hidraw4"

# Feature report 0x04 for controller AA:BB:CC:DD:EE:FF with host 11:22:33:44:55:66
# Controller MAC LSB-first: FF EE DD CC BB AA
# Host MAC LSB-first: 66 55 44 33 22 11
SAMPLE_FEATURE_REPORT = bytes(
    [0x04, 0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA, 0x00, 0x00, 0x00, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11]
)

# Zero host (unpaired)
SAMPLE_FEATURE_REPORT_UNPAIRED = bytes(
    [0x04, 0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
)


def _make_dev_info(
    path: bytes = FAKE_PATH_1,
    vendor_id: int = 0x054C,
    product_id: int = 0x03D5,
    interface_number: int = 0,
) -> dict:
    """Create a fake hid.enumerate() device info dict."""
    return {
        "path": path,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "interface_number": interface_number,
        "serial_number": "",
    }


@pytest.fixture
def usb_pairing(mock_tracer):
    """Provide USBPairing instance for tests."""
    return USBPairing(mock_tracer)


class TestGetUSBControllers:
    """Tests for get_usb_controllers()."""

    @patch("psmove_pairing.usb_pairing.hid")
    def test_no_controllers_connected(self, mock_hid, usb_pairing):
        """Test when no HID devices are found."""
        mock_hid.enumerate.return_value = []

        controllers = usb_pairing.get_usb_controllers()
        assert controllers == []

    @patch("psmove_pairing.usb_pairing.hid")
    def test_usb_controller_detected(self, mock_hid, usb_pairing):
        """Test when a USB controller is detected via feature report."""
        # enumerate is called once per product ID; return device for first PID only
        mock_hid.enumerate.side_effect = lambda _v, p: [_make_dev_info()] if p == 0x03D5 else []

        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = SAMPLE_FEATURE_REPORT
        mock_hid.device.return_value = mock_device

        controllers = usb_pairing.get_usb_controllers()
        assert len(controllers) == 1
        assert controllers[0] == (FAKE_PATH_1, "AA:BB:CC:DD:EE:FF")
        mock_device.open_path.assert_called_once_with(FAKE_PATH_1)
        mock_device.close.assert_called_once()

    @patch("psmove_pairing.usb_pairing.hid")
    def test_bluetooth_controller_excluded(self, mock_hid, usb_pairing):
        """Test that Bluetooth controllers (interface_number == -1) are excluded."""
        mock_hid.enumerate.side_effect = lambda _v, p: [_make_dev_info(interface_number=-1)] if p == 0x03D5 else []

        controllers = usb_pairing.get_usb_controllers()
        assert controllers == []
        # Should not attempt to open BT devices
        mock_hid.device.assert_not_called()

    @patch("psmove_pairing.usb_pairing.hid")
    def test_multiple_controllers(self, mock_hid, usb_pairing):
        """Test multiple USB controllers enumerated with BT filtered out."""
        # Two controllers for one PID: one USB, one BT (filtered out)
        mock_hid.enumerate.side_effect = (
            lambda _v, p: [
                _make_dev_info(path=FAKE_PATH_1, interface_number=0),
                _make_dev_info(path=FAKE_PATH_2, interface_number=-1),
            ]
            if p == 0x03D5
            else []
        )

        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = SAMPLE_FEATURE_REPORT
        mock_hid.device.return_value = mock_device

        controllers = usb_pairing.get_usb_controllers()
        assert len(controllers) == 1  # BT one filtered out

    @patch("psmove_pairing.usb_pairing.hid")
    def test_hid_error_skips_device(self, mock_hid, usb_pairing):
        """Test that HID errors during enumeration skip the device gracefully."""
        mock_hid.enumerate.side_effect = lambda _v, p: [_make_dev_info()] if p == 0x03D5 else []

        mock_device = MagicMock()
        mock_device.get_feature_report.side_effect = OSError("Device busy")
        mock_hid.device.return_value = mock_device

        controllers = usb_pairing.get_usb_controllers()
        assert controllers == []
        mock_device.close.assert_called_once()

    @patch("psmove_pairing.usb_pairing.hid")
    def test_feature_report_returned_as_list(self, mock_hid, usb_pairing):
        """Test that list-type feature report (hidraw quirk) is handled."""
        mock_hid.enumerate.side_effect = lambda _v, p: [_make_dev_info()] if p == 0x03D5 else []

        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = list(SAMPLE_FEATURE_REPORT)
        mock_hid.device.return_value = mock_device

        controllers = usb_pairing.get_usb_controllers()
        assert len(controllers) == 1
        assert controllers[0] == (FAKE_PATH_1, "AA:BB:CC:DD:EE:FF")


class TestPairController:
    """Tests for pair_controller()."""

    @pytest.mark.asyncio
    @patch("psmove_pairing.usb_pairing.hid")
    async def test_successful_pairing(self, mock_hid, usb_pairing):
        """Test successful pairing writes host address."""
        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = SAMPLE_FEATURE_REPORT_UNPAIRED
        mock_hid.device.return_value = mock_device

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is True
        mock_device.open_path.assert_called_once_with(FAKE_PATH_1)
        mock_device.send_feature_report.assert_called_once()
        mock_device.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("psmove_pairing.usb_pairing.hid")
    async def test_pairing_already_set(self, mock_hid, usb_pairing):
        """Test pairing when host address already matches (still succeeds)."""
        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = SAMPLE_FEATURE_REPORT
        mock_hid.device.return_value = mock_device

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is True
        # Still writes (idempotent)
        mock_device.send_feature_report.assert_called_once()

    @pytest.mark.asyncio
    @patch("psmove_pairing.usb_pairing.hid")
    async def test_hid_error_returns_false(self, mock_hid, usb_pairing):
        """Test that HID errors during pairing return False."""
        mock_device = MagicMock()
        mock_device.open_path.side_effect = OSError("Device not found")
        mock_hid.device.return_value = mock_device

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False

    @pytest.mark.asyncio
    @patch("psmove_pairing.usb_pairing.hid")
    async def test_send_feature_report_error(self, mock_hid, usb_pairing):
        """Test that send_feature_report errors return False."""
        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = SAMPLE_FEATURE_REPORT_UNPAIRED
        mock_device.send_feature_report.side_effect = OSError("Write failed")
        mock_hid.device.return_value = mock_device

        result = await usb_pairing.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False
        mock_device.close.assert_called_once()


class TestRestartBluetoothService:
    """Tests for restart_bluetooth_service()."""

    @pytest.mark.asyncio
    async def test_restarts_via_systemd_dbus(self, usb_pairing):
        """Test that bluetooth service is restarted via D-Bus systemd interface."""
        with patch("psmove_pairing.usb_pairing.restart_systemd_unit", new_callable=AsyncMock) as mock_restart:
            with patch("asyncio.sleep", return_value=None):
                with patch("psmove_pairing.usb_pairing.register_pairing_agent", new_callable=AsyncMock):
                    await usb_pairing.restart_bluetooth_service()
                    mock_restart.assert_called_once_with("bluetooth.service")

    @pytest.mark.asyncio
    async def test_re_registers_agent_after_restart(self, usb_pairing):
        """Test that the pairing agent is re-registered after BT service restart.

        BlueZ service restart invalidates the previous agent's D-Bus session.
        Without re-registration, incoming connections from unknown controllers
        are rejected, causing the light to blink and stop.
        """
        with patch("psmove_pairing.usb_pairing.restart_systemd_unit", new_callable=AsyncMock):
            with patch("asyncio.sleep", return_value=None):
                with patch(
                    "psmove_pairing.usb_pairing.register_pairing_agent", new_callable=AsyncMock
                ) as mock_agent:
                    await usb_pairing.restart_bluetooth_service()
                    mock_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_handles_dbus_failure(self, usb_pairing):
        """Test that D-Bus failure is logged but doesn't raise."""
        with patch(
            "psmove_pairing.usb_pairing.restart_systemd_unit",
            new_callable=AsyncMock,
            side_effect=Exception("D-Bus error"),
        ):
            with patch("psmove_pairing.usb_pairing.register_pairing_agent", new_callable=AsyncMock):
                # Should not raise
                await usb_pairing.restart_bluetooth_service()


class TestBluezTrustController:
    """Tests for bluez_trust_controller()."""

    @pytest.mark.asyncio
    async def test_trust_succeeds(self, usb_pairing):
        """Test successful trust."""
        runner = MockCommandRunner()
        runner.add_response(["bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"], (0, "trust succeeded"))

        with patch("psmove_pairing.usb_pairing.run_command", runner):
            result = await usb_pairing.bluez_trust_controller("AA:BB:CC:DD:EE:FF")
            assert result is True

    @pytest.mark.asyncio
    async def test_trust_retries_on_failure(self, usb_pairing):
        """Test that trust retries once after failure."""
        call_count = 0

        async def mock_run_command(cmd):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (1, "Unable to open mgmt_socket")
            return (0, "trust succeeded")

        with patch("psmove_pairing.usb_pairing.run_command", mock_run_command):
            with patch("asyncio.sleep", return_value=None):
                result = await usb_pairing.bluez_trust_controller("AA:BB:CC:DD:EE:FF")
                assert result is True
                assert call_count == 2

    @pytest.mark.asyncio
    async def test_trust_failure_is_non_fatal(self, usb_pairing):
        """Test that trust failure returns False but doesn't raise."""
        runner = MockCommandRunner()
        runner.add_response(["bluetoothctl", "trust", "AA:BB:CC:DD:EE:FF"], (1, "failed"))

        with patch("psmove_pairing.usb_pairing.run_command", runner):
            with patch("asyncio.sleep", return_value=None):
                result = await usb_pairing.bluez_trust_controller("AA:BB:CC:DD:EE:FF")
                assert result is False


class TestProcessController:
    """Tests for process_controller()."""

    @pytest.mark.asyncio
    async def test_already_in_bluez_runs_full_pairing(self, usb_pairing):
        """Test that 'already paired' controllers still go through pairing to verify host address."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=False)
        adapter = AdapterInfo(hci="hci0", address="11:22:33:44:55:66", name="adapter-hci0", device_count=0)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=adapter)
        usb_pairing.pair_controller = AsyncMock(return_value=True)
        usb_pairing.restart_bluetooth_service = AsyncMock()
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)

        result = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")

        assert result is True
        usb_pairing.pair_controller.assert_called_once_with(FAKE_PATH_1, "00:06:F7:AA:BB:CC", "11:22:33:44:55:66")
        usb_pairing.restart_bluetooth_service.assert_called_once()
        usb_pairing.bluez_trust_controller.assert_called_once()

    @pytest.mark.asyncio
    async def test_verified_serial_skipped_on_subsequent_poll(self, usb_pairing):
        """Test that a controller verified this session is skipped on next poll."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=True)
        adapter = AdapterInfo(hci="hci0", address="11:22:33:44:55:66", name="adapter-hci0", device_count=0)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=adapter)
        usb_pairing.pair_controller = AsyncMock(return_value=True)
        usb_pairing.restart_bluetooth_service = AsyncMock()
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)

        # First call: full pairing
        result1 = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")
        assert result1 is True

        # Second call: should skip
        result2 = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")
        assert result2 is False
        # pair_controller only called once (first time)
        usb_pairing.pair_controller.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_adapters_available(self, usb_pairing):
        """Test when no Bluetooth adapters are available."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=True)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=None)

        result = await usb_pairing.process_controller(FAKE_PATH_1, "00:06:F7:AA:BB:CC")
        assert result is False


class TestPoll:
    """Tests for poll()."""

    @pytest.mark.asyncio
    @patch("psmove_pairing.usb_pairing.hid")
    async def test_poll_increments_count(self, mock_hid, usb_pairing):
        """Test that poll count is incremented."""
        mock_hid.enumerate.side_effect = lambda _v, _p: []

        initial_count = usb_pairing.poll_count
        await usb_pairing.poll()
        assert usb_pairing.poll_count == initial_count + 1

    @pytest.mark.asyncio
    @patch("psmove_pairing.usb_pairing.hid")
    async def test_poll_skips_when_no_controllers(self, mock_hid, usb_pairing):
        """Test that poll skips processing when no USB controllers found."""
        mock_hid.enumerate.side_effect = lambda _v, _p: []

        await usb_pairing.poll()
        # No process_controller calls expected
