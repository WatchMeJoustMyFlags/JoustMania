"""Tests for the unpair control API (UC-005): USBPairing.unpair + HTTP mapping."""

import asyncio
import threading
from unittest.mock import AsyncMock, create_autospec, patch

import pytest

from lib.bluez_dbus import DBusError
from psmove_pairing import registrations
from psmove_pairing.adapter_manager import AdapterInfo
from psmove_pairing.control_api import handle_post, unpair_response
from psmove_pairing.usb_pairing import USBPairing

from .conftest import FAKE_PATH_1

CONTROLLER = "00:06:F7:AA:BB:CC"
ADAPTER_0 = "AA:00:00:00:00:00"
ADAPTER_1 = "BB:00:00:00:00:00"


@pytest.fixture
def usb_pairing(mock_tracer):
    return USBPairing(mock_tracer)


@pytest.fixture
def remove():
    """The shared BlueZ helper, autospecced; default: removed on two adapters."""
    mock = create_autospec(registrations.remove_controller_registrations, return_value=[ADAPTER_0, ADAPTER_1])
    with patch("psmove_pairing.usb_pairing.remove_controller_registrations", mock):
        yield mock


class TestUnpairResponse:
    @pytest.mark.uc("UC-005", flow="main", steps=[4, 5, 6, 7], br=["BR-001"])
    async def test_unpair_removes_everywhere_and_forgets_session_pairing(self, usb_pairing, remove):
        """200 with the removed adapters; PS Move only; the serial is no longer 'verified this session'."""
        usb_pairing._verified_serials = {CONTROLLER.lower(), "00:06:F7:11:22:33"}

        status, body = await unpair_response(usb_pairing, CONTROLLER.lower())

        assert status == 200
        assert body == {"address": CONTROLLER, "removed": 2, "adapters": [ADAPTER_0, ADAPTER_1]}
        remove.assert_awaited_once_with(CONTROLLER)  # require_psmove defaults to True (BR-001)
        assert usb_pairing._verified_serials == {"00:06:F7:11:22:33"}

    @pytest.mark.uc("UC-005", flow="main", steps=[6])
    async def test_unpaired_controller_is_paired_again_over_usb(self, usb_pairing, remove):
        """After an unpair, plugging the controller in pairs it again instead of skipping it (UC-001 A1)."""
        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.select_least_loaded_adapter = lambda: AdapterInfo("hci0", ADAPTER_0, "a", 0)
        usb_pairing.pair_controller = AsyncMock(return_value=True)
        usb_pairing.restart_bluetooth_service = AsyncMock()
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)
        assert await usb_pairing.process_controller(FAKE_PATH_1, CONTROLLER) is True

        await unpair_response(usb_pairing, CONTROLLER)

        assert await usb_pairing.process_controller(FAKE_PATH_1, CONTROLLER) is True
        assert usb_pairing.pair_controller.await_count == 2

    @pytest.mark.uc("UC-005", flow="A4")
    async def test_unknown_serial_is_404(self, usb_pairing, remove):
        remove.return_value = []

        status, body = await unpair_response(usb_pairing, CONTROLLER)

        assert status == 404
        assert "not registered" in body["error"]

    @pytest.mark.uc("UC-005", flow="A5")
    async def test_mock_serial_is_400_and_bluez_untouched(self, usb_pairing, remove):
        status, body = await unpair_response(usb_pairing, "MOCK0001")

        assert status == 400
        assert "not a Bluetooth address" in body["error"]
        remove.assert_not_awaited()

    @pytest.mark.uc("UC-005", flow="A6")
    async def test_controller_being_paired_is_409(self, usb_pairing, remove):
        """While process_controller pairs this controller, an unpair is refused and nothing is removed."""
        in_pairing = asyncio.Event()
        release = asyncio.Event()

        async def slow_pair(*_args):
            in_pairing.set()
            await release.wait()
            return True

        usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
        usb_pairing.adapter_manager.select_least_loaded_adapter = lambda: AdapterInfo("hci0", ADAPTER_0, "a", 0)
        usb_pairing.pair_controller = slow_pair
        usb_pairing.restart_bluetooth_service = AsyncMock()
        usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)
        usb_pairing.remove_stale_registrations = AsyncMock(return_value=[])
        pairing = asyncio.create_task(usb_pairing.process_controller(FAKE_PATH_1, CONTROLLER))
        await in_pairing.wait()

        status, _body = await unpair_response(usb_pairing, CONTROLLER.lower())
        other_status, _ = await unpair_response(usb_pairing, "00:06:F7:11:22:33")
        release.set()
        await pairing

        assert status == 409
        assert other_status == 200  # another controller can still be unpaired
        remove.assert_awaited_once_with("00:06:F7:11:22:33")
        assert (await unpair_response(usb_pairing, CONTROLLER))[0] == 200  # pairing finished

    @pytest.mark.uc("UC-005", flow="A7")
    async def test_bluez_failure_is_503(self, usb_pairing, remove):
        remove.side_effect = DBusError("org.freedesktop.DBus.Error.ServiceUnknown", ["bluez down"])

        status, body = await unpair_response(usb_pairing, CONTROLLER)

        assert status == 503
        assert "BlueZ unavailable" in body["error"]


class TestHandlePost:
    """The HTTP thread schedules the unpair on the daemon's event loop."""

    @pytest.fixture
    def daemon_loop(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        yield loop
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()

    @pytest.mark.uc("UC-005", flow="main", steps=[4, 7])
    def test_post_unpair_runs_on_daemon_loop(self, usb_pairing, remove, daemon_loop):
        status, body = handle_post("/controllers/00%3A06%3AF7%3AAA%3ABB%3ACC/unpair", usb_pairing, daemon_loop)

        assert status == 200
        assert body["removed"] == 2
        remove.assert_awaited_once_with(CONTROLLER)

    def test_other_paths_are_not_handled(self, usb_pairing, daemon_loop):
        assert handle_post("/healthz", usb_pairing, daemon_loop) is None
        assert handle_post("/controllers/x/forget", usb_pairing, daemon_loop) is None

    @pytest.mark.uc("UC-005", flow="A7")
    def test_daemon_not_running_is_503(self, usb_pairing):
        assert handle_post(f"/controllers/{CONTROLLER}/unpair", usb_pairing, None)[0] == 503
