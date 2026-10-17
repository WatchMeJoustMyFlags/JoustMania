"""Tests for the unpair control API (UC-005): USBPairing.unpair + HTTP mapping."""

import asyncio
import threading
from unittest.mock import AsyncMock, create_autospec, patch

import pytest

import server
from lib.bluez_dbus import DBusError
from psmove_pairing import registrations
from psmove_pairing.adapter_manager import AdapterInfo
from psmove_pairing.control_api import handle_post, unpair_response
from psmove_pairing.pairing_backend import HidapiBackend
from psmove_pairing.registrations import PsMoveRegistration, RemovalResult
from psmove_pairing.usb_pairing import USBPairing

from .conftest import FAKE_PATH_1, FAKE_PATH_2

CONTROLLER = "00:06:F7:AA:BB:CC"
OTHER = "00:06:F7:11:22:33"
ADAPTER_0 = "AA:00:00:00:00:00"
ADAPTER_1 = "BB:00:00:00:00:00"


def result(adapter: str, outcome: str, error: str | None = None) -> RemovalResult:
    registration = PsMoveRegistration(adapter, CONTROLLER, hci="hci0", device_path=f"/org/bluez/hci0/dev_{adapter[:2]}")
    return RemovalResult(registration, outcome, error)


@pytest.fixture
def usb_pairing(mock_tracer):
    return USBPairing(mock_tracer)


@pytest.fixture
def remove():
    """The shared BlueZ helper, autospecced; default: removed on two adapters."""
    mock = create_autospec(
        registrations.remove_controller_registrations,
        return_value=[result(ADAPTER_0, "removed"), result(ADAPTER_1, "removed")],
    )
    with patch("psmove_pairing.usb_pairing.remove_controller_registrations", mock):
        yield mock


def stub_pairing_steps(usb_pairing: USBPairing, pair=None) -> None:
    usb_pairing.adapter_manager.refresh_adapters = AsyncMock()
    usb_pairing.adapter_manager.select_least_loaded_adapter = lambda: AdapterInfo("hci0", ADAPTER_0, "a", 0)
    usb_pairing.pair_controller = pair or AsyncMock(return_value=True)
    usb_pairing.restart_bluetooth_service = AsyncMock()
    usb_pairing.bluez_trust_controller = AsyncMock(return_value=True)
    usb_pairing.remove_stale_registrations = AsyncMock(return_value=[])


async def poll_with_usb(usb_pairing: USBPairing, serials: list[str], path: bytes = FAKE_PATH_1) -> None:
    """One poll() where the backend reports these serials on USB (at ``path``)."""
    backend = create_autospec(HidapiBackend, instance=True)
    backend.get_usb_controllers.return_value = [(path, s) for s in serials]
    usb_pairing._current_backend = None  # poll() caches its backend; use this one
    with (
        patch("psmove_pairing.usb_pairing._resolve_routing", return_value="python"),
        patch("psmove_pairing.usb_pairing._create_backend", return_value=backend),
    ):
        await usb_pairing.poll()


class TestUnpairResponse:
    @pytest.mark.uc("UC-005", flow="main", steps=[4, 5, 6, 7], br=["BR-001"])
    async def test_unpair_removes_everywhere_and_reports_results(self, usb_pairing, remove):
        """200 with per-registration counts; PS Move only (BR-001); no longer 'verified this session'."""
        usb_pairing._verified_serials = {CONTROLLER.lower(), OTHER}

        status, body = await unpair_response(usb_pairing, CONTROLLER.lower())

        assert status == 200
        assert body == {
            "address": CONTROLLER,
            "removed": 2,
            "already_absent": 0,
            "failed": 0,
            "adapters": [ADAPTER_0, ADAPTER_1],
            "results": [{"adapter": ADAPTER_0, "outcome": "removed"}, {"adapter": ADAPTER_1, "outcome": "removed"}],
            "on_usb": False,
        }
        remove.assert_awaited_once_with(CONTROLLER)  # require_psmove defaults to True (BR-001)
        assert usb_pairing._verified_serials == {OTHER}

    @pytest.mark.uc("UC-005", flow="main", steps=[6])
    async def test_controller_not_on_usb_is_paired_again_when_plugged_in(self, usb_pairing, remove):
        """Unpaired while off USB: the next USB plug pairs it again (not UC-001 A1)."""
        stub_pairing_steps(usb_pairing)
        await poll_with_usb(usb_pairing, [CONTROLLER])  # paired once
        await poll_with_usb(usb_pairing, [])  # unplugged

        status, body = await unpair_response(usb_pairing, CONTROLLER)
        await poll_with_usb(usb_pairing, [])
        await poll_with_usb(usb_pairing, [CONTROLLER])  # plugged in again

        assert (status, body["on_usb"]) == (200, False)
        assert usb_pairing.pair_controller.await_count == 2

    @pytest.mark.uc("UC-005", flow="A8")
    async def test_controller_on_usb_is_not_repaired_until_unplugged_and_replugged(self, usb_pairing, remove):
        """A8: no immediate re-pair (its BlueZ restart would disconnect every controller)."""
        stub_pairing_steps(usb_pairing)
        await poll_with_usb(usb_pairing, [CONTROLLER])
        assert usb_pairing.pair_controller.await_count == 1

        status, body = await unpair_response(usb_pairing, CONTROLLER)
        await poll_with_usb(usb_pairing, [CONTROLLER])
        await poll_with_usb(usb_pairing, [CONTROLLER])

        assert (status, body["on_usb"]) == (200, True)
        assert usb_pairing.pair_controller.await_count == 1
        usb_pairing.restart_bluetooth_service.assert_awaited_once()

        await poll_with_usb(usb_pairing, [])  # unplugged
        await poll_with_usb(usb_pairing, [CONTROLLER])  # plugged back in
        assert usb_pairing.pair_controller.await_count == 2

    @pytest.mark.uc("UC-005", flow="A4")
    async def test_unknown_serial_is_404(self, usb_pairing, remove):
        """A4: 404 only when the scan finds no registration at all."""
        remove.return_value = []

        status, body = await unpair_response(usb_pairing, CONTROLLER)

        assert status == 404
        assert "not registered" in body["error"]
        assert CONTROLLER not in usb_pairing._awaiting_replug

    @pytest.mark.uc("UC-005", flow="A4", br=["BR-002"])
    async def test_all_already_absent_is_idempotent_success(self, usb_pairing, remove):
        """Found but every removal 'already absent' (a race with BlueZ): 200, removed 0."""
        remove.return_value = [result(ADAPTER_0, "already_absent"), result(ADAPTER_1, "already_absent")]

        status, body = await unpair_response(usb_pairing, CONTROLLER)

        assert status == 200
        assert (body["removed"], body["already_absent"], body["failed"]) == (0, 2, 0)

    @pytest.mark.uc("UC-005", flow="A5")
    async def test_mock_serial_is_400_and_bluez_untouched(self, usb_pairing, remove):
        status, body = await unpair_response(usb_pairing, "MOCK0001")

        assert status == 400
        assert "not a Bluetooth address" in body["error"]
        remove.assert_not_awaited()

    @pytest.mark.uc("UC-005", flow="A6")
    async def test_unpair_during_a_pairing_is_409(self, usb_pairing, remove):
        """While any pairing holds the lock (selection through trust), unpair is refused, removing nothing."""
        in_pairing = asyncio.Event()
        release = asyncio.Event()

        async def slow_pair(*_args):
            in_pairing.set()
            await release.wait()
            return True

        stub_pairing_steps(usb_pairing, pair=slow_pair)
        pairing = asyncio.create_task(usb_pairing.process_controller(FAKE_PATH_1, OTHER))
        await in_pairing.wait()

        statuses = [(await unpair_response(usb_pairing, a))[0] for a in (CONTROLLER, OTHER)]
        release.set()
        await pairing

        assert statuses == [409, 409]
        remove.assert_not_awaited()
        assert (await unpair_response(usb_pairing, CONTROLLER))[0] == 200  # pairing finished

    @pytest.mark.uc("UC-005", flow="A6")
    async def test_second_unpair_while_first_runs_is_409(self, usb_pairing, remove):
        """A double click: the second request is refused while the first holds the lock."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_remove(*_args, **_kwargs):
            started.set()
            await release.wait()
            return [result(ADAPTER_0, "removed")]

        remove.side_effect = slow_remove
        first = asyncio.create_task(unpair_response(usb_pairing, CONTROLLER))
        await started.wait()
        second = await unpair_response(usb_pairing, CONTROLLER)
        release.set()

        assert second[0] == 409
        assert (await first)[0] == 200

    @pytest.mark.uc("UC-005", flow="A6")
    async def test_pairing_waits_for_a_running_unpair(self, usb_pairing, remove):
        """process_controller does not select an adapter or restart BlueZ in the middle of an unpair."""
        release = asyncio.Event()
        order: list[str] = []

        async def slow_remove(*_args, **_kwargs):
            order.append("unpair start")
            await release.wait()
            order.append("unpair end")
            return [result(ADAPTER_0, "removed")]

        async def pair(*_args):
            order.append("pair")
            return True

        remove.side_effect = slow_remove
        stub_pairing_steps(usb_pairing, pair=pair)
        unpair = asyncio.create_task(unpair_response(usb_pairing, CONTROLLER))
        await asyncio.sleep(0)
        pairing = asyncio.create_task(usb_pairing.process_controller(FAKE_PATH_1, OTHER))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(unpair, pairing)

        assert order == ["unpair start", "unpair end", "pair"]

    @pytest.mark.uc("UC-005", flow="A8")
    async def test_poll_waiting_on_the_lock_does_not_pair_the_unpaired_controller(self, usb_pairing, remove):
        """A poll already in process_controller for the same controller re-checks A8 under the lock."""
        release = asyncio.Event()

        async def slow_remove(*_args, **_kwargs):
            await release.wait()
            return [result(ADAPTER_0, "removed")]

        remove.side_effect = slow_remove
        stub_pairing_steps(usb_pairing)
        usb_pairing._usb_paths = {CONTROLLER: FAKE_PATH_1}  # on USB
        unpair = asyncio.create_task(unpair_response(usb_pairing, CONTROLLER))
        await asyncio.sleep(0)
        pairing = asyncio.create_task(usb_pairing.process_controller(FAKE_PATH_1, CONTROLLER))
        await asyncio.sleep(0)
        release.set()
        (status, body), paired = await asyncio.gather(unpair, pairing)

        assert (status, body["on_usb"]) == (200, True)
        assert paired is False
        usb_pairing.pair_controller.assert_not_awaited()
        usb_pairing.restart_bluetooth_service.assert_not_awaited()

    @pytest.mark.uc("UC-005", flow="A8")
    async def test_replug_within_one_poll_is_noticed_by_a_new_device_path(self, usb_pairing, remove):
        """Unplugged and plugged back in between two polls: a new hidraw path re-arms pairing."""
        stub_pairing_steps(usb_pairing)
        await poll_with_usb(usb_pairing, [CONTROLLER])
        await unpair_response(usb_pairing, CONTROLLER)

        await poll_with_usb(usb_pairing, [CONTROLLER])  # same path: still waiting
        assert usb_pairing.pair_controller.await_count == 1
        await poll_with_usb(usb_pairing, [CONTROLLER], path=FAKE_PATH_2)  # replugged, new path
        assert usb_pairing.pair_controller.await_count == 2

    @pytest.mark.uc("UC-005", flow="A6")
    async def test_hung_backend_releases_the_pairing_lock(self, usb_pairing, remove):
        """A pair_controller that never answers times out, fails the pairing and frees the lock."""
        hung = asyncio.Event()
        backend = create_autospec(HidapiBackend, instance=True)

        async def never(*_args):
            await hung.wait()

        backend.pair_controller.side_effect = never
        stub_pairing_steps(usb_pairing)
        del usb_pairing.pair_controller  # use the real one, with its deadline
        usb_pairing._current_backend, usb_pairing._current_backend_name = backend, "python"

        with patch("psmove_pairing.usb_pairing.PAIR_TIMEOUT_SECONDS", 0.05):
            assert await usb_pairing.process_controller(FAKE_PATH_1, OTHER) is False

        assert not usb_pairing._pairing_lock.locked()
        assert (await unpair_response(usb_pairing, CONTROLLER))[0] == 200

    @pytest.mark.uc("UC-005", flow="A7")
    async def test_partial_failure_is_503_with_results(self, usb_pairing, remove):
        remove.return_value = [
            result(ADAPTER_0, "removed"),
            result(ADAPTER_1, "failed", "org.bluez.Error.Failed: busy"),
        ]

        status, body = await unpair_response(usb_pairing, CONTROLLER)

        assert status == 503
        assert (body["removed"], body["failed"]) == (1, 1)
        assert body["results"][1] == {
            "adapter": ADAPTER_1,
            "outcome": "failed",
            "error": "org.bluez.Error.Failed: busy",
        }
        assert "could not be removed" in body["error"]

    @pytest.mark.uc("UC-005", flow="A7")
    async def test_bluez_error_is_503(self, usb_pairing, remove):
        remove.side_effect = DBusError("org.freedesktop.DBus.Error.AccessDenied", ["denied"])

        status, body = await unpair_response(usb_pairing, CONTROLLER)

        assert status == 503
        assert "AccessDenied" in body["error"]

    @pytest.mark.uc("UC-005", flow="A7")
    async def test_unexpected_error_is_503_not_a_dropped_connection(self, usb_pairing, remove):
        remove.side_effect = PermissionError("/var/run/dbus/system_bus_socket")

        status, body = await unpair_response(usb_pairing, CONTROLLER)

        assert status == 503
        assert "PermissionError" in body["error"]


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


class TestControlBind:
    """UC-005 BR-003: the HTTP port binds only the CONTROL_BIND addresses."""

    @pytest.mark.uc("UC-005", br=["BR-003"])
    def test_default_is_loopback_only(self):
        assert server.bind_addresses(None) == ["127.0.0.1"]
        assert server.bind_addresses(" ") == ["127.0.0.1"]
        assert server.bind_addresses("127.0.0.1, 172.17.0.1") == ["127.0.0.1", "172.17.0.1"]

    @pytest.mark.uc("UC-005", br=["BR-003"])
    def test_binds_each_address_and_skips_unbindable_ones(self):
        servers = server.start_health_server(0, ["127.0.0.1", "192.0.2.123"])  # TEST-NET: not local
        try:
            assert [s.server_address[0] for s in servers] == ["127.0.0.1"]
        finally:
            for s in servers:
                s.shutdown()
                s.server_close()
