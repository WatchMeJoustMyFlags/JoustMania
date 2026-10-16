"""Tests for Bluetooth adapter load balancing (UC-001 BR-001).

Drives the production AdapterManager (the wrapper usb_pairing uses, delegating
to adapter_manager_modern) against a fake BlueZ ObjectManager tree. Only the
two D-Bus calls are patched.
"""

from unittest.mock import AsyncMock, patch

import pytest

from lib.bluez_dbus import DBusError
from psmove_pairing.adapter_manager import AdapterManager

from .conftest import bluez_tree, devices

HCI0 = "AA:00:00:00:00:00"
HCI1 = "BB:00:00:00:00:00"
HCI2 = "CC:00:00:00:00:00"


async def refreshed_manager(tree: dict) -> AdapterManager:
    manager = AdapterManager()
    with (
        patch("psmove_pairing.adapter_manager_modern.get_system_bus", AsyncMock()),
        patch("psmove_pairing.adapter_manager_modern.get_managed_objects", AsyncMock(return_value=tree)),
    ):
        await manager.refresh_adapters()
    return manager


class TestAdapterLoadBalancing:
    """UC-001 step 3 / BR-001: new controllers go to the least-loaded adapter."""

    @pytest.mark.uc("UC-001", flow="main", steps=[3], br=["BR-001"])
    async def test_selects_adapter_with_fewest_devices(self):
        """The adapter with the fewest BlueZ devices is chosen (allowed + rejected adapters)."""
        manager = await refreshed_manager(
            bluez_tree({"hci0": (HCI0, devices(3)), "hci1": (HCI1, devices(1)), "hci2": (HCI2, devices(2))})
        )

        adapter = manager.select_least_loaded_adapter()

        assert adapter is not None
        assert (adapter.hci, adapter.address, adapter.device_count) == ("hci1", HCI1, 1)
        assert manager.get_lowest_bt_device() == HCI1

    @pytest.mark.uc("UC-001", flow="main", steps=[3], br=["BR-001"])
    async def test_load_counts_every_bluez_known_device(self):
        """Load is every Device1 BlueZ knows on the adapter: paired-but-disconnected and non-PS-Move count too."""
        not_connected_moves = devices(2, connected=False)
        connected_headset = [{"Address": "11:22:33:44:55:66", "Connected": True, "Name": "Headset"}]
        manager = await refreshed_manager(
            bluez_tree(
                {
                    # 2 disconnected PS Moves + 1 connected headset = load 3
                    "hci0": (HCI0, not_connected_moves + connected_headset),
                    # 2 connected PS Moves = load 2
                    "hci1": (HCI1, devices(2, connected=True, prefix="00:07:F7")),
                }
            )
        )

        adapter = manager.select_least_loaded_adapter()

        assert adapter.address == HCI1
        assert adapter.device_count == 2

    @pytest.mark.uc("UC-001", flow="main", steps=[3], br=["BR-001"])
    async def test_tie_goes_to_lowest_adapter_address(self):
        """Equal load: the lowest adapter address wins, regardless of hci numbering."""
        manager = await refreshed_manager(bluez_tree({"hci0": (HCI2, devices(1)), "hci1": (HCI0, devices(1))}))

        adapter = manager.select_least_loaded_adapter()

        assert (adapter.hci, adapter.address) == ("hci1", HCI0)

    @pytest.mark.uc("UC-001", flow="main", steps=[3], br=["BR-001"])
    async def test_no_capacity_cap_is_enforced(self):
        """About 7 per adapter is a hardware recommendation only: a full adapter is still selected."""
        manager = await refreshed_manager(bluez_tree({"hci0": (HCI0, devices(9)), "hci1": (HCI1, devices(8))}))

        adapter = manager.select_least_loaded_adapter()

        assert adapter.address == HCI1
        assert adapter.device_count == 8

    async def test_devices_are_attributed_to_their_own_adapter(self):
        """A device under /org/bluez/hci1 never counts towards hci10 (path prefix match)."""
        manager = await refreshed_manager(
            bluez_tree({"hci1": (HCI1, devices(1)), "hci10": (HCI0, devices(3, prefix="00:08:F7"))})
        )

        loads = adapter_loads(manager)

        assert loads == {HCI1: 1, HCI0: 3}

    async def test_no_adapters_selects_nothing(self):
        """Without any Adapter1 object there is nothing to select."""
        manager = await refreshed_manager({"/org/bluez": {}})

        assert manager.select_least_loaded_adapter() is None
        assert manager.get_lowest_bt_device() == ""

    async def test_dbus_error_means_no_adapters(self):
        """A D-Bus failure while listing adapters leaves no adapter to select (pairing fails, retried next poll)."""
        manager = AdapterManager()
        with (
            patch("psmove_pairing.adapter_manager_modern.get_system_bus", AsyncMock()),
            patch(
                "psmove_pairing.adapter_manager_modern.get_managed_objects",
                AsyncMock(side_effect=DBusError("org.freedesktop.DBus.Error.ServiceUnknown", [])),
            ),
        ):
            await manager.refresh_adapters()

        assert manager.select_least_loaded_adapter() is None


class TestCheckIfNotPaired:
    """UC-001 A4 input: whether BlueZ already knows the controller."""

    async def test_known_controller_is_detected_case_insensitively(self):
        manager = await refreshed_manager(bluez_tree({"hci0": (HCI0, [{"Address": "00:06:F7:AA:BB:CC"}])}))

        assert manager.check_if_not_paired("00:06:f7:aa:bb:cc") is False
        assert manager.check_if_not_paired("00:06:F7:00:00:01") is True


def adapter_loads(manager: AdapterManager) -> dict[str, int]:
    """Per-adapter load as the selection sees it."""
    return {addr: len(devs) for addr, devs in manager._delegate._bt_devices.items()}
