"""Tests for psmove_pairing.usb_pairing module."""

from unittest.mock import MagicMock, patch

import pytest

from psmove_pairing.usb_pairing import USBPairing

from .conftest import MockCommandRunner


@pytest.fixture
def usb_pairing(mock_tracer):
    """Provide USBPairing instance for tests."""
    return USBPairing(mock_tracer, "/usr/bin/psmove")


class TestGetUSBControllersPsmove:
    """Tests for get_usb_controllers_psmove()."""

    def test_no_controllers_connected(self, usb_pairing, mock_psmove_module):
        """Test when no controllers are connected."""
        mock_psmove_module.count_connected.return_value = 0

        controllers = usb_pairing.get_usb_controllers_psmove()
        assert controllers == []

    def test_usb_controller_detected(self, usb_pairing, mock_psmove_module):
        """Test when USB controller is detected."""
        mock_psmove_module.count_connected.return_value = 1

        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_USB
        mock_move.get_serial.return_value = "aa:bb:cc:dd:ee:ff"
        mock_psmove_module.PSMove.return_value = mock_move

        controllers = usb_pairing.get_usb_controllers_psmove()
        assert len(controllers) == 1
        assert controllers[0] == (0, "AA:BB:CC:DD:EE:FF")

    def test_bluetooth_controller_excluded(self, usb_pairing, mock_psmove_module):
        """Test that Bluetooth controllers are excluded."""
        mock_psmove_module.count_connected.return_value = 1

        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_Bluetooth
        mock_psmove_module.PSMove.return_value = mock_move

        controllers = usb_pairing.get_usb_controllers_psmove()
        assert controllers == []


class TestPairControllerPsmove:
    """Tests for pair_controller_psmove()."""

    def test_successful_pairing(self, usb_pairing, mock_psmove_module):
        """Test successful pairing via pair_custom."""
        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_USB
        mock_move.pair_custom.return_value = True
        mock_psmove_module.PSMove.return_value = mock_move

        result = usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is True
        mock_move.pair_custom.assert_called_once_with("11:22:33:44:55:66")

    def test_pairing_failure(self, usb_pairing, mock_psmove_module):
        """Test pairing failure."""
        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_USB
        mock_move.pair_custom.return_value = False
        mock_psmove_module.PSMove.return_value = mock_move

        result = usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False

    def test_not_usb_connected(self, usb_pairing, mock_psmove_module):
        """Test when controller is not USB connected."""
        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_Bluetooth
        mock_psmove_module.PSMove.return_value = mock_move

        result = usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False

    def test_exception_handling(self, usb_pairing, mock_psmove_module):
        """Test exception handling during pairing."""
        mock_psmove_module.PSMove.side_effect = RuntimeError("Hardware error")

        result = usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False


class TestCalibrateController:
    """Tests for calibrate_controller()."""

    @pytest.mark.asyncio
    async def test_successful_calibration(self, usb_pairing):
        """Test successful calibration."""
        runner = MockCommandRunner()
        runner.add_response(["/usr/bin/psmove", "calibrate"], (0, "Calibration complete"))

        with patch("psmove_pairing.usb_pairing.run_command", runner):
            result = await usb_pairing.calibrate_controller("00:06:F7:AA:BB:CC")
            assert result is True

    @pytest.mark.asyncio
    async def test_calibration_failure(self, usb_pairing):
        """Test calibration failure (non-critical)."""
        runner = MockCommandRunner()
        runner.add_response(["/usr/bin/psmove", "calibrate"], (1, "Calibration failed"))

        with patch("psmove_pairing.usb_pairing.run_command", runner):
            result = await usb_pairing.calibrate_controller("00:06:F7:AA:BB:CC")
            assert result is False


class TestResetBluetoothAdapter:
    """Tests for reset_bluetooth_adapter()."""

    @pytest.mark.asyncio
    async def test_power_cycles_adapter(self, usb_pairing):
        """Test that adapter is power-cycled via DBus."""
        mock_iface = MagicMock()
        mock_iface.Get.return_value = True  # Adapter is powered

        with patch("psmove_pairing.usb_pairing.get_hci_dict", return_value={"hci0": "AA:BB:CC:DD:EE:FF"}):
            with patch("psmove_pairing.usb_pairing._get_adapter_proxy") as mock_proxy:
                with patch("psmove_pairing.usb_pairing.dbus") as mock_dbus_mod:
                    mock_dbus_mod.Interface.return_value = mock_iface
                    with patch("asyncio.sleep", return_value=None):
                        await usb_pairing.reset_bluetooth_adapter()
                        # Should have called Set twice (off then on)
                        assert mock_iface.Set.call_count == 2

    @pytest.mark.asyncio
    async def test_handles_dbus_failure(self, usb_pairing):
        """Test that DBus failure is logged but doesn't raise."""
        with patch("psmove_pairing.usb_pairing.get_hci_dict", return_value={"hci0": "AA:BB:CC:DD:EE:FF"}):
            with patch("psmove_pairing.usb_pairing._get_adapter_proxy", side_effect=Exception("DBus error")):
                # Should not raise
                await usb_pairing.reset_bluetooth_adapter()


class TestProcessController:
    """Tests for process_controller()."""

    @pytest.mark.asyncio
    async def test_skip_already_paired(self, usb_pairing):
        """Test skipping controller already paired."""
        usb_pairing.adapter_manager.refresh_adapters = MagicMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=False)

        result = await usb_pairing.process_controller(0, "00:06:F7:AA:BB:CC")
        assert result is False

    @pytest.mark.asyncio
    async def test_no_adapters_available(self, usb_pairing):
        """Test when no Bluetooth adapters are available."""
        usb_pairing.adapter_manager.refresh_adapters = MagicMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=True)
        usb_pairing.adapter_manager.select_least_loaded_adapter = MagicMock(return_value=None)

        result = await usb_pairing.process_controller(0, "00:06:F7:AA:BB:CC")
        assert result is False


class TestPoll:
    """Tests for poll()."""

    @pytest.mark.asyncio
    async def test_poll_increments_count(self, usb_pairing, mock_psmove_module):
        """Test that poll count is incremented."""
        mock_psmove_module.count_connected.return_value = 0

        initial_count = usb_pairing.poll_count
        await usb_pairing.poll()
        assert usb_pairing.poll_count == initial_count + 1

    @pytest.mark.asyncio
    async def test_poll_skips_when_no_controllers(self, usb_pairing, mock_psmove_module):
        """Test that poll skips processing when no USB controllers found."""
        mock_psmove_module.count_connected.return_value = 0

        await usb_pairing.poll()
        # No process_controller calls expected
