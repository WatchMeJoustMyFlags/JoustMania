"""
Unit tests for CentralizedBTDiscovery.

Tests multi-adapter enumeration, scanning, affinity tracking, and hot-plug.
Default discovery mode is "hidapi".
"""

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery


@pytest.fixture
def discovery():
    return CentralizedBTDiscovery()


@pytest.fixture
def bluez_discovery():
    """Discovery with explicit bluez mode for testing the BlueZ scanning path."""
    return CentralizedBTDiscovery(discovery_mode="bluez")


class TestInitialize:
    """Tests for adapter enumeration and enabling."""

    @pytest.mark.asyncio
    async def test_enumerates_and_enables_all_adapters(self, discovery):
        """Should enumerate adapters, enable each, set metric, and return dict."""
        adapters = {"hci0": "AA:BB:CC:DD:EE:00", "hci1": "AA:BB:CC:DD:EE:01"}

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch("services.controller_manager.multiplexer.bt_discovery.metrics") as mock_metrics,
        ):
            mock_bt.get_hci_dict = AsyncMock(return_value=adapters)
            mock_bt.enable_adapter = AsyncMock()

            result = await discovery.initialize()

        assert result == adapters
        assert discovery.adapter_count == 2
        assert discovery.adapters == adapters
        assert mock_bt.enable_adapter.call_count == 2
        mock_metrics.bluetooth_adapter_count.set.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_no_adapters_returns_empty(self, discovery):
        """Should return empty dict and warn when no adapters found."""
        with patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt:
            mock_bt.get_hci_dict = AsyncMock(return_value={})

            result = await discovery.initialize()

        assert result == {}
        assert discovery.adapter_count == 0

    @pytest.mark.asyncio
    async def test_partial_enable_failure(self, discovery):
        """One adapter failing enable should not prevent others."""
        adapters = {"hci0": "AA:BB:CC:DD:EE:00", "hci1": "AA:BB:CC:DD:EE:01"}

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch("services.controller_manager.multiplexer.bt_discovery.metrics") as mock_metrics,
        ):
            mock_bt.get_hci_dict = AsyncMock(return_value=adapters)
            mock_bt.enable_adapter = AsyncMock(side_effect=[Exception("rfkill error"), None])

            result = await discovery.initialize()

        assert result == adapters
        assert discovery.adapter_count == 2
        # Both adapters were attempted
        assert mock_bt.enable_adapter.call_count == 2
        mock_metrics.bluetooth_adapter_count.set.assert_called_once_with(2)


class TestGetAllAttachedAddresses:
    """Tests for consolidated address scanning via BlueZ path."""

    @pytest.mark.asyncio
    async def test_scans_all_adapters_and_deduplicates(self, bluez_discovery):
        """Should scan each adapter and deduplicate addresses."""
        bluez_discovery._adapters = {"hci0": "AA:00", "hci1": "BB:00"}

        with patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt:
            mock_bt.get_attached_addresses = AsyncMock(
                side_effect=[
                    ["00:06:F7:AA:BB:CC", "00:06:F7:DD:EE:FF"],
                    ["00:06:F7:DD:EE:FF", "00:06:F7:11:22:33"],  # DD:EE:FF is duplicate
                ]
            )

            addresses = await bluez_discovery.get_all_attached_addresses()

        assert len(addresses) == 3
        assert "00:06:F7:AA:BB:CC" in addresses
        assert "00:06:F7:DD:EE:FF" in addresses
        assert "00:06:F7:11:22:33" in addresses

    @pytest.mark.asyncio
    async def test_builds_affinity_map(self, bluez_discovery):
        """Should track which adapter owns which address."""
        bluez_discovery._adapters = {"hci0": "AA:00", "hci1": "BB:00"}

        with patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt:
            mock_bt.get_attached_addresses = AsyncMock(
                side_effect=[
                    ["00:06:F7:AA:BB:CC"],
                    ["00:06:F7:DD:EE:FF"],
                ]
            )

            await bluez_discovery.get_all_attached_addresses()

        assert bluez_discovery.get_adapter_for_address("00:06:F7:AA:BB:CC") == "hci0"
        assert bluez_discovery.get_adapter_for_address("00:06:F7:DD:EE:FF") == "hci1"

    @pytest.mark.asyncio
    async def test_scan_failure_on_one_adapter_continues(self, bluez_discovery):
        """Failure scanning one adapter should not prevent scanning others."""
        bluez_discovery._adapters = {"hci0": "AA:00", "hci1": "BB:00"}

        with patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt:
            mock_bt.get_attached_addresses = AsyncMock(
                side_effect=[
                    Exception("D-Bus error"),
                    ["00:06:F7:DD:EE:FF"],
                ]
            )

            addresses = await bluez_discovery.get_all_attached_addresses()

        assert addresses == ["00:06:F7:DD:EE:FF"]

    @pytest.mark.asyncio
    async def test_empty_adapters_returns_empty(self, discovery):
        """No adapters should return empty list."""
        addresses = await discovery.get_all_attached_addresses()
        assert addresses == []


class TestGetAdapterForAddress:
    """Tests for adapter affinity lookup."""

    def test_returns_correct_adapter(self, discovery):
        # Keys are stored normalized (uppercase, no colons)
        discovery._address_to_adapter = {"AABB": "hci0", "CCDD": "hci1"}
        assert discovery.get_adapter_for_address("AA:BB") == "hci0"
        assert discovery.get_adapter_for_address("CC:DD") == "hci1"

    def test_normalizes_input_address(self, discovery):
        """Both colon and no-colon formats should find the same entry."""
        discovery._address_to_adapter = {"0006F7AABBCC": "hci0"}
        assert discovery.get_adapter_for_address("00:06:F7:AA:BB:CC") == "hci0"
        assert discovery.get_adapter_for_address("0006F7AABBCC") == "hci0"

    def test_returns_none_for_unknown(self, discovery):
        assert discovery.get_adapter_for_address("unknown") is None


class TestRefreshAdapters:
    """Tests for hot-plug adapter refresh."""

    @pytest.mark.asyncio
    async def test_detects_new_adapter(self, discovery):
        """Should detect and enable newly plugged-in adapters."""
        discovery._adapters = {"hci0": "AA:00"}

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch("services.controller_manager.multiplexer.bt_discovery.metrics") as mock_metrics,
        ):
            mock_bt.get_hci_dict = AsyncMock(return_value={"hci0": "AA:00", "hci1": "BB:00"})
            mock_bt.enable_adapter = AsyncMock()

            new = await discovery.refresh_adapters()

        assert new == ["hci1"]
        mock_bt.enable_adapter.assert_called_once_with("hci1")
        assert discovery.adapter_count == 2
        mock_metrics.bluetooth_adapter_count.set.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_adapter_removal(self, discovery):
        """Removed adapter should be cleaned from dict."""
        discovery._adapters = {"hci0": "AA:00", "hci1": "BB:00"}

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch("services.controller_manager.multiplexer.bt_discovery.metrics") as mock_metrics,
        ):
            mock_bt.get_hci_dict = AsyncMock(return_value={"hci0": "AA:00"})
            mock_bt.enable_adapter = AsyncMock()

            new = await discovery.refresh_adapters()

        assert new == []
        assert discovery.adapter_count == 1
        assert "hci1" not in discovery.adapters
        mock_bt.enable_adapter.assert_not_called()
        mock_metrics.bluetooth_adapter_count.set.assert_called_once_with(1)

    @pytest.mark.asyncio
    async def test_no_changes(self, discovery):
        """No new or removed adapters should return empty list."""
        discovery._adapters = {"hci0": "AA:00"}

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch("services.controller_manager.multiplexer.bt_discovery.metrics"),
        ):
            mock_bt.get_hci_dict = AsyncMock(return_value={"hci0": "AA:00"})
            mock_bt.enable_adapter = AsyncMock()

            new = await discovery.refresh_adapters()

        assert new == []
        mock_bt.enable_adapter.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_adapter_enable_failure(self, discovery):
        """Failure to enable new adapter should not prevent updating adapter list."""
        discovery._adapters = {"hci0": "AA:00"}

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch("services.controller_manager.multiplexer.bt_discovery.metrics"),
        ):
            mock_bt.get_hci_dict = AsyncMock(return_value={"hci0": "AA:00", "hci1": "BB:00"})
            mock_bt.enable_adapter = AsyncMock(side_effect=Exception("rfkill"))

            new = await discovery.refresh_adapters()

        assert new == ["hci1"]
        assert discovery.adapter_count == 2  # Still tracked even if enable failed


class TestDiscoveryMode:
    """Tests for discovery_mode parameter."""

    def test_default_mode_is_hidapi(self):
        d = CentralizedBTDiscovery()
        assert d.discovery_mode == "hidapi"

    def test_explicit_hidapi_mode(self):
        d = CentralizedBTDiscovery(discovery_mode="hidapi")
        assert d.discovery_mode == "hidapi"

    def test_normalize_address(self):
        assert CentralizedBTDiscovery._normalize_address("00:06:F7:AA:BB:CC") == "0006F7AABBCC"
        assert CentralizedBTDiscovery._normalize_address("0006F7AABBCC") == "0006F7AABBCC"
        assert CentralizedBTDiscovery._normalize_address("aa:bb") == "AABB"


class TestHidapiMode:
    """Tests for hidapi discovery mode."""

    @pytest.mark.asyncio
    async def test_scan_via_hidapi_enumerates_devices(self, discovery):
        """Should use hid.enumerate to find PS Move controllers."""
        discovery._adapters = {"hci0": "AA:00"}

        mock_hid = MagicMock()
        mock_hid.enumerate.side_effect = [
            [{"serial_number": "00:06:F7:AA:BB:CC"}, {"serial_number": "00:06:F7:DD:EE:FF"}],
            [],  # ZCM2 returns nothing
            [],  # ZCM2E returns nothing
        ]

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch.dict("sys.modules", {"hidraw": mock_hid}),
            patch("services.controller_manager.multiplexer.bt_discovery.hid", mock_hid, create=True),
        ):
            mock_bt.get_attached_addresses = AsyncMock(return_value=["00:06:F7:AA:BB:CC", "00:06:F7:DD:EE:FF"])
            addresses = await discovery.get_all_attached_addresses()

        assert len(addresses) == 2
        assert "0006F7AABBCC" in addresses
        assert "0006F7DDEEFF" in addresses

    @pytest.mark.asyncio
    async def test_scan_via_hidapi_builds_affinity(self, discovery):
        """Should cross-reference HID serials with BlueZ for adapter affinity."""
        discovery._adapters = {"hci0": "AA:00", "hci1": "BB:00"}

        mock_hid = MagicMock()
        mock_hid.enumerate.side_effect = [
            [{"serial_number": "0006F7AABBCC"}, {"serial_number": "0006F7DDEEFF"}],
            [],  # ZCM2
            [],  # ZCM2E
        ]

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch.dict("sys.modules", {"hidraw": mock_hid}),
            patch("services.controller_manager.multiplexer.bt_discovery.hid", mock_hid, create=True),
        ):
            mock_bt.get_attached_addresses = AsyncMock(
                side_effect=[
                    ["00:06:F7:AA:BB:CC"],  # hci0 has first controller
                    ["00:06:F7:DD:EE:FF"],  # hci1 has second controller
                ]
            )
            await discovery.get_all_attached_addresses()

        assert discovery.get_adapter_for_address("0006F7AABBCC") == "hci0"
        assert discovery.get_adapter_for_address("0006F7DDEEFF") == "hci1"

    @pytest.mark.asyncio
    async def test_scan_via_hidapi_deduplicates(self, discovery):
        """Should deduplicate controllers seen on multiple HID paths."""
        discovery._adapters = {"hci0": "AA:00"}

        mock_hid = MagicMock()
        mock_hid.enumerate.side_effect = [
            [{"serial_number": "0006F7AABBCC"}, {"serial_number": "0006F7AABBCC"}],  # Duplicate
            [],  # ZCM2
            [],  # ZCM2E
        ]

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch.dict("sys.modules", {"hidraw": mock_hid}),
            patch("services.controller_manager.multiplexer.bt_discovery.hid", mock_hid, create=True),
        ):
            mock_bt.get_attached_addresses = AsyncMock(return_value=["00:06:F7:AA:BB:CC"])
            addresses = await discovery.get_all_attached_addresses()

        assert len(addresses) == 1

    @pytest.mark.asyncio
    async def test_sysfs_fallback_when_bluez_has_no_devices(self, discovery):
        """Should resolve adapter affinity via sysfs when BlueZ cross-ref fails."""
        discovery._adapters = {"hci0": "AA:00", "hci1": "BB:00"}

        mock_hid = MagicMock()
        mock_hid.enumerate.side_effect = [
            [
                {"serial_number": "0006F7AABBCC", "path": b"/dev/hidraw0"},
                {"serial_number": "0006F7DDEEFF", "path": b"/dev/hidraw1"},
            ],
            [],  # ZCM2
            [],  # ZCM2E
        ]

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch.dict("sys.modules", {"hidraw": mock_hid}),
            patch("services.controller_manager.multiplexer.bt_discovery.hid", mock_hid, create=True),
            patch.object(
                CentralizedBTDiscovery,
                "_resolve_adapter_from_sysfs",
                side_effect=lambda path: "hci0" if b"hidraw0" in path else "hci1",
            ),
        ):
            # BlueZ returns nothing — devices connected via hidraw, not BlueZ
            mock_bt.get_attached_addresses = AsyncMock(return_value=[])
            await discovery.get_all_attached_addresses()

        assert discovery.get_adapter_for_address("0006F7AABBCC") == "hci0"
        assert discovery.get_adapter_for_address("0006F7DDEEFF") == "hci1"

    @pytest.mark.asyncio
    async def test_sysfs_fallback_skips_unknown_adapters(self, discovery):
        """Sysfs fallback should ignore adapters not in the known adapter list."""
        discovery._adapters = {"hci0": "AA:00"}

        mock_hid = MagicMock()
        mock_hid.enumerate.side_effect = [
            [{"serial_number": "0006F7AABBCC", "path": b"/dev/hidraw0"}],
            [],
            [],
        ]

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch.dict("sys.modules", {"hidraw": mock_hid}),
            patch("services.controller_manager.multiplexer.bt_discovery.hid", mock_hid, create=True),
            patch.object(
                CentralizedBTDiscovery,
                "_resolve_adapter_from_sysfs",
                return_value="hci9",  # Not in _adapters
            ),
        ):
            mock_bt.get_attached_addresses = AsyncMock(return_value=[])
            await discovery.get_all_attached_addresses()

        assert discovery.get_adapter_for_address("0006F7AABBCC") is None

    def test_resolve_adapter_from_sysfs_parses_hci(self):
        """Should extract hci name from resolved sysfs path."""
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch(
                "pathlib.Path.resolve",
                return_value=pathlib.Path(
                    "/sys/devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0"
                    "/bluetooth/hci0/hci0:11/0005:054C:03D5.0001/hidraw/hidraw0"
                ),
            ),
        ):
            result = CentralizedBTDiscovery._resolve_adapter_from_sysfs(b"/dev/hidraw0")
        assert result == "hci0"

    def test_resolve_adapter_from_sysfs_no_hci(self):
        """Should return None when sysfs path has no hci adapter (e.g. USB device)."""
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch(
                "pathlib.Path.resolve",
                return_value=pathlib.Path(
                    "/sys/devices/pci0000:00/0000:00:14.0/usb1/1-1/1-1:1.0/0003:054C:03D5.0001/hidraw/hidraw0"
                ),
            ),
        ):
            result = CentralizedBTDiscovery._resolve_adapter_from_sysfs(b"/dev/hidraw0")
        assert result is None

    def test_resolve_adapter_from_sysfs_missing(self):
        """Should return None when sysfs path doesn't exist."""
        with patch("pathlib.Path.exists", return_value=False):
            result = CentralizedBTDiscovery._resolve_adapter_from_sysfs(b"/dev/hidraw99")
        assert result is None

    @pytest.mark.asyncio
    async def test_bluez_mode_uses_bluez_scanning(self, bluez_discovery):
        """Explicit bluez mode should NOT call hid.enumerate."""
        bluez_discovery._adapters = {"hci0": "AA:00"}

        with patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt:
            mock_bt.get_attached_addresses = AsyncMock(return_value=["00:06:F7:AA:BB:CC"])
            addresses = await bluez_discovery.get_all_attached_addresses()

        assert len(addresses) == 1
        assert "00:06:F7:AA:BB:CC" in addresses
