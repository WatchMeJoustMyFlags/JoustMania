"""Tests for psmove_pairing.usb_pairing module."""

from unittest.mock import AsyncMock, MagicMock, patch

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

    @pytest.mark.asyncio
    async def test_successful_pairing(self, usb_pairing, mock_psmove_module):
        """Test successful pairing via subprocess."""
        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_USB
        mock_psmove_module.PSMove.return_value = mock_move

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"OK\n", b"")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
            assert result is True

    @pytest.mark.asyncio
    async def test_pairing_failure(self, usb_pairing, mock_psmove_module):
        """Test pairing failure."""
        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_USB
        mock_psmove_module.PSMove.return_value = mock_move

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"FAIL\n", b"")

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
            assert result is False

    @pytest.mark.asyncio
    async def test_not_usb_connected(self, usb_pairing, mock_psmove_module):
        """Test when controller is not USB connected."""
        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_Bluetooth
        mock_psmove_module.PSMove.return_value = mock_move

        result = await usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout_treated_as_success(self, usb_pairing, mock_psmove_module):
        """Test that timeout (ZCM2 PIN agent blocking) is treated as success."""
        mock_move = MagicMock()
        mock_move.connection_type = mock_psmove_module.Conn_USB
        mock_psmove_module.PSMove.return_value = mock_move

        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = TimeoutError()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
            assert result is True
            mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_handling(self, usb_pairing, mock_psmove_module):
        """Test exception handling during pairing."""
        mock_psmove_module.PSMove.side_effect = RuntimeError("Hardware error")

        result = await usb_pairing.pair_controller_psmove(0, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
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


class TestRestartBluetoothService:
    """Tests for restart_bluetooth_service()."""

    @pytest.mark.asyncio
    async def test_restarts_via_systemd_dbus(self, usb_pairing):
        """Test that bluetooth service is restarted via D-Bus systemd interface."""
        with patch("psmove_pairing.usb_pairing.restart_systemd_unit", new_callable=AsyncMock) as mock_restart:
            with patch("asyncio.sleep", return_value=None):
                await usb_pairing.restart_bluetooth_service()
                mock_restart.assert_called_once_with("bluetooth.service")

    @pytest.mark.asyncio
    async def test_handles_dbus_failure(self, usb_pairing):
        """Test that D-Bus failure is logged but doesn't raise."""
        with patch(
            "psmove_pairing.usb_pairing.restart_systemd_unit",
            new_callable=AsyncMock,
            side_effect=Exception("D-Bus error"),
        ):
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
    async def test_skip_already_paired_but_still_trusts(self, usb_pairing):
        """Test skipping pairing for already-paired controller but still trusting."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.check_if_not_paired = MagicMock(return_value=False)
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)

        result = await usb_pairing.process_controller(0, "00:06:F7:AA:BB:CC")
        assert result is False
        usb_pairing.bluez_trust_controller.assert_called_once_with("00:06:F7:AA:BB:CC")

    @pytest.mark.asyncio
    async def test_no_adapters_available(self, usb_pairing):
        """Test when no Bluetooth adapters are available."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
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
