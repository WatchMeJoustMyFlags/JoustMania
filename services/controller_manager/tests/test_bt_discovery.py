"""
Unit tests for CentralizedBTDiscovery.

Tests multi-adapter enumeration, scanning, affinity tracking, and hot-plug.
"""

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.controller_manager.multiplexer.bt_discovery import CentralizedBTDiscovery


def _mock_pairing_resolve(discovery, serials, mapping):
    """Helper to simulate _resolve_via_pairing_dir for integration tests."""
    count = 0
    for serial in serials:
        if serial not in discovery._address_to_adapter and serial in mapping:
            discovery._address_to_adapter[serial] = mapping[serial]
            count += 1
    return count


@pytest.fixture
def discovery():
    return CentralizedBTDiscovery()


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
    """Tests for normalize_address utility."""

    def test_normalize_address(self):
        assert CentralizedBTDiscovery._normalize_address("00:06:F7:AA:BB:CC") == "0006F7AABBCC"
        assert CentralizedBTDiscovery._normalize_address("0006F7AABBCC") == "0006F7AABBCC"
        assert CentralizedBTDiscovery._normalize_address("aa:bb") == "AABB"


class TestHidapiMode:
    """Tests for hidapi discovery."""

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
    async def test_pairing_dir_fallback_when_bluez_has_no_devices(self, discovery):
        """Should resolve adapter affinity via /var/lib/bluetooth/ when BlueZ cross-ref fails."""
        discovery._adapters = {"hci0": "AA:BB:CC:DD:EE:00", "hci1": "AA:BB:CC:DD:EE:01"}

        mock_hid = MagicMock()
        mock_hid.enumerate.side_effect = [
            [
                {"serial_number": "0006F7AABBCC"},
                {"serial_number": "0006F7DDEEFF"},
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
                "_resolve_via_pairing_dir",
                side_effect=lambda serials: _mock_pairing_resolve(
                    discovery,
                    serials,
                    {
                        "0006F7AABBCC": "hci0",
                        "0006F7DDEEFF": "hci1",
                    },
                ),
            ),
        ):
            # BlueZ returns nothing — devices connected via uhid, not BlueZ D-Bus
            mock_bt.get_attached_addresses = AsyncMock(return_value=[])
            await discovery.get_all_attached_addresses()

        assert discovery.get_adapter_for_address("0006F7AABBCC") == "hci0"
        assert discovery.get_adapter_for_address("0006F7DDEEFF") == "hci1"

    @pytest.mark.asyncio
    async def test_pairing_dir_fallback_no_match(self, discovery):
        """Pairing dir fallback should leave affinity unset when no match found."""
        discovery._adapters = {"hci0": "AA:BB:CC:DD:EE:00"}

        mock_hid = MagicMock()
        mock_hid.enumerate.side_effect = [
            [{"serial_number": "0006F7AABBCC"}],
            [],
            [],
        ]

        with (
            patch("services.controller_manager.multiplexer.bt_discovery.bluetooth") as mock_bt,
            patch.dict("sys.modules", {"hidraw": mock_hid}),
            patch("services.controller_manager.multiplexer.bt_discovery.hid", mock_hid, create=True),
            patch.object(
                CentralizedBTDiscovery,
                "_resolve_via_pairing_dir",
                return_value=0,  # No matches found
            ),
        ):
            mock_bt.get_attached_addresses = AsyncMock(return_value=[])
            await discovery.get_all_attached_addresses()

        assert discovery.get_adapter_for_address("0006F7AABBCC") is None


class TestResolvePairingDir:
    """Tests for _resolve_via_pairing_dir."""

    def test_resolves_from_pairing_directory(self, discovery, tmp_path):
        """Should find adapter for device via /var/lib/bluetooth/<adapter>/<device>/ dirs."""
        discovery._adapters = {"hci0": "AA:BB:CC:DD:EE:00", "hci1": "AA:BB:CC:DD:EE:01"}

        # Create fake pairing directories
        adapter0_dir = tmp_path / "AA:BB:CC:DD:EE:00"
        adapter0_dir.mkdir()
        (adapter0_dir / "00:06:F7:AA:BB:CC").mkdir()  # Device paired on hci0
        (adapter0_dir / "cache").mkdir()
        (adapter0_dir / "settings").touch()

        adapter1_dir = tmp_path / "AA:BB:CC:DD:EE:01"
        adapter1_dir.mkdir()
        (adapter1_dir / "00:06:F7:DD:EE:FF").mkdir()  # Device paired on hci1

        with patch(
            "services.controller_manager.multiplexer.bt_discovery.pathlib.Path",
            side_effect=lambda p: tmp_path if p == "/var/lib/bluetooth" else pathlib.Path(p),
        ):
            count = discovery._resolve_via_pairing_dir({"0006F7AABBCC", "0006F7DDEEFF"})

        assert count == 2
        assert discovery.get_adapter_for_address("0006F7AABBCC") == "hci0"
        assert discovery.get_adapter_for_address("0006F7DDEEFF") == "hci1"

    def test_skips_already_resolved(self, discovery, tmp_path):
        """Should not overwrite existing adapter affinity."""
        discovery._adapters = {"hci0": "AA:BB:CC:DD:EE:00", "hci1": "AA:BB:CC:DD:EE:01"}
        discovery._address_to_adapter["0006F7AABBCC"] = "hci1"  # Already resolved

        adapter0_dir = tmp_path / "AA:BB:CC:DD:EE:00"
        adapter0_dir.mkdir()
        (adapter0_dir / "00:06:F7:AA:BB:CC").mkdir()

        with patch(
            "services.controller_manager.multiplexer.bt_discovery.pathlib.Path",
            side_effect=lambda p: tmp_path if p == "/var/lib/bluetooth" else pathlib.Path(p),
        ):
            count = discovery._resolve_via_pairing_dir({"0006F7AABBCC"})

        assert count == 0
        assert discovery.get_adapter_for_address("0006F7AABBCC") == "hci1"  # Unchanged

    def test_returns_zero_when_dir_missing(self, discovery, tmp_path):
        """Should return 0 when /var/lib/bluetooth doesn't exist."""
        discovery._adapters = {"hci0": "AA:BB:CC:DD:EE:00"}
        nonexistent = tmp_path / "nonexistent"

        with patch(
            "services.controller_manager.multiplexer.bt_discovery.pathlib.Path",
            return_value=nonexistent,
        ):
            count = discovery._resolve_via_pairing_dir({"0006F7AABBCC"})

        assert count == 0

    def test_ignores_unknown_adapter_addresses(self, discovery, tmp_path):
        """Should skip adapter directories not in the known adapter list."""
        discovery._adapters = {"hci0": "AA:BB:CC:DD:EE:00"}

        # Device paired on unknown adapter
        unknown_dir = tmp_path / "FF:FF:FF:FF:FF:FF"
        unknown_dir.mkdir()
        (unknown_dir / "00:06:F7:AA:BB:CC").mkdir()

        with patch(
            "services.controller_manager.multiplexer.bt_discovery.pathlib.Path",
            side_effect=lambda p: tmp_path if p == "/var/lib/bluetooth" else pathlib.Path(p),
        ):
            count = discovery._resolve_via_pairing_dir({"0006F7AABBCC"})

        assert count == 0
        assert discovery.get_adapter_for_address("0006F7AABBCC") is None
