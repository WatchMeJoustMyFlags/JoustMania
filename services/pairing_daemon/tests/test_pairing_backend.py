"""Tests for pairing backend implementations."""

from unittest.mock import MagicMock, patch

import pytest

from psmove_pairing.pairing_backend import HidapiBackend, RustServiceBackend

# Reuse test data from test_usb_pairing
FAKE_PATH_1 = b"/dev/hidraw3"
FAKE_PATH_2 = b"/dev/hidraw4"

SAMPLE_FEATURE_REPORT = bytes(
    [0x04, 0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA, 0x00, 0x00, 0x00, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11]
)

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


class TestHidapiBackendGetUSBControllers:
    """Tests for HidapiBackend.get_usb_controllers()."""

    @patch("psmove_pairing.pairing_backend.hid")
    def test_no_controllers(self, mock_hid):
        """Returns empty list when no HID devices found."""
        mock_hid.enumerate.return_value = []
        backend = HidapiBackend()
        assert backend.get_usb_controllers() == []

    @patch("psmove_pairing.pairing_backend.hid")
    def test_usb_controller_detected(self, mock_hid):
        """Detects USB controller and reads serial from feature report."""
        mock_hid.enumerate.side_effect = lambda _v, p: [_make_dev_info()] if p == 0x03D5 else []

        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = SAMPLE_FEATURE_REPORT
        mock_hid.device.return_value = mock_device

        backend = HidapiBackend()
        controllers = backend.get_usb_controllers()

        assert len(controllers) == 1
        assert controllers[0] == (FAKE_PATH_1, "AA:BB:CC:DD:EE:FF")
        mock_device.open_path.assert_called_once_with(FAKE_PATH_1)
        mock_device.close.assert_called_once()

    @patch("psmove_pairing.pairing_backend.hid")
    def test_bluetooth_excluded(self, mock_hid):
        """Excludes Bluetooth devices (interface_number == -1)."""
        mock_hid.enumerate.side_effect = lambda _v, p: [_make_dev_info(interface_number=-1)] if p == 0x03D5 else []

        backend = HidapiBackend()
        controllers = backend.get_usb_controllers()

        assert controllers == []
        mock_hid.device.assert_not_called()

    @patch("psmove_pairing.pairing_backend.hid")
    def test_hid_error_skips_device(self, mock_hid):
        """Skips devices that raise errors during feature report read."""
        mock_hid.enumerate.side_effect = lambda _v, p: [_make_dev_info()] if p == 0x03D5 else []

        mock_device = MagicMock()
        mock_device.get_feature_report.side_effect = OSError("Device busy")
        mock_hid.device.return_value = mock_device

        backend = HidapiBackend()
        controllers = backend.get_usb_controllers()

        assert controllers == []
        mock_device.close.assert_called_once()

    @patch("psmove_pairing.pairing_backend.hid")
    def test_feature_report_as_list(self, mock_hid):
        """Handles feature report returned as list (hidraw quirk)."""
        mock_hid.enumerate.side_effect = lambda _v, p: [_make_dev_info()] if p == 0x03D5 else []

        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = list(SAMPLE_FEATURE_REPORT)
        mock_hid.device.return_value = mock_device

        backend = HidapiBackend()
        controllers = backend.get_usb_controllers()

        assert len(controllers) == 1
        assert controllers[0] == (FAKE_PATH_1, "AA:BB:CC:DD:EE:FF")


class TestHidapiBackendPairController:
    """Tests for HidapiBackend.pair_controller()."""

    @pytest.mark.asyncio
    @patch("psmove_pairing.pairing_backend.hid")
    async def test_successful_pairing(self, mock_hid):
        """Writes host address via feature report."""
        mock_device = MagicMock()
        mock_device.get_feature_report.return_value = SAMPLE_FEATURE_REPORT_UNPAIRED
        mock_hid.device.return_value = mock_device

        backend = HidapiBackend()
        result = await backend.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")

        assert result is True
        mock_device.open_path.assert_called_once_with(FAKE_PATH_1)
        mock_device.send_feature_report.assert_called_once()
        mock_device.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("psmove_pairing.pairing_backend.hid")
    async def test_hid_error_returns_false(self, mock_hid):
        """Returns False on HID error."""
        mock_device = MagicMock()
        mock_device.open_path.side_effect = OSError("Device not found")
        mock_hid.device.return_value = mock_device

        backend = HidapiBackend()
        result = await backend.pair_controller(FAKE_PATH_1, "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")

        assert result is False


class TestRustServiceBackend:
    """Tests for RustServiceBackend stub."""

    def test_get_usb_controllers_raises(self):
        """All methods raise NotImplementedError."""
        backend = RustServiceBackend()
        with pytest.raises(NotImplementedError, match="Rust pairing service not yet implemented"):
            backend.get_usb_controllers()

    @pytest.mark.asyncio
    async def test_pair_controller_raises(self):
        """pair_controller raises NotImplementedError."""
        backend = RustServiceBackend()
        with pytest.raises(NotImplementedError, match="Rust pairing service not yet implemented"):
            await backend.pair_controller(b"/dev/hidraw0", "AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66")
