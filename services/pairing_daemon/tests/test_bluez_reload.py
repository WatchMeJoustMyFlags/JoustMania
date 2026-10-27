"""pairing.bluez_restart_mode policy and the primary game-state read (UC-001 BR-005, A6; #1446)."""

import asyncio
import http.server
import io
import json
import threading
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from psmove_pairing import bluez_reload
from psmove_pairing.bluez_records import RecordOutcome
from psmove_pairing.bluez_reload import BluezReloader, PendingRecord, primary_game_busy

pytestmark = pytest.mark.asyncio


def _reloader(busy: bool = False) -> tuple[BluezReloader, AsyncMock, AsyncMock]:
    game_busy = AsyncMock(return_value=busy)
    restart = AsyncMock(return_value=None)
    return BluezReloader(game_busy=game_busy, restart=restart), game_busy, restart


def _mode(mode: str):
    return patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value=mode)


ADAPTER = "AA:BB:CC:DD:EE:01"
CONTROLLER = "00:06:F7:AA:BB:CC"


def _record(bluetooth_dir: Path | None = None, controller: str = CONTROLLER) -> PendingRecord:
    return PendingRecord(ADAPTER, controller, 0x03D5, bluetooth_dir or Path("/nonexistent-bluez-storage"))


async def _after_pairing(reloader: BluezReloader, record_written: bool) -> str:
    """One pairing (request) followed by the poll's single run_pending."""
    reloader.request(_record() if record_written else None)
    return await reloader.run_pending()


class TestBluezReloader:
    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize("record_written", [False, True])
    async def test_never_restarts_nothing(self, record_written):
        reloader, game_busy, restart = _reloader()
        with _mode("never"):
            assert await _after_pairing(reloader, record_written) == "skipped"
        restart.assert_not_awaited()
        game_busy.assert_not_awaited()
        assert reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize("record_written", [False, True])
    async def test_always_restarts_even_during_a_game(self, record_written):
        reloader, game_busy, restart = _reloader(busy=True)
        with _mode("always"):
            assert await _after_pairing(reloader, record_written) == "restarted"
        restart.assert_awaited_once()
        game_busy.assert_not_awaited()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_when_idle_needs_a_new_device_record(self):
        reloader, game_busy, restart = _reloader()
        with _mode("when_idle"):
            assert await _after_pairing(reloader, False) == "skipped"
        restart.assert_not_awaited()
        game_busy.assert_not_awaited()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_when_idle_restarts_when_no_real_game_runs(self):
        reloader, _game_busy, restart = _reloader(busy=False)
        with _mode("when_idle"):
            assert await _after_pairing(reloader, True) == "restarted"
        restart.assert_awaited_once()
        assert reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_when_idle_defers_during_a_real_game_and_runs_after_it(self):
        reloader, game_busy, restart = _reloader(busy=True)
        with _mode("when_idle"):
            assert await _after_pairing(reloader, True) == "deferred"
            assert reloader.pending is True
            assert await reloader.run_pending() == "deferred"
            restart.assert_not_awaited()

            game_busy.return_value = False  # the real game ended
            assert await reloader.run_pending() == "restarted"
        restart.assert_awaited_once()
        assert reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_switching_to_never_drops_a_pending_restart(self):
        reloader, _game_busy, restart = _reloader(busy=True)
        with _mode("when_idle"):
            await _after_pairing(reloader, True)
        with _mode("never"):
            assert await reloader.run_pending() == "skipped"
        assert reloader.pending is False
        restart.assert_not_awaited()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_failed_restart_is_logged_not_raised(self):
        reloader, _game_busy, restart = _reloader()
        restart.side_effect = RuntimeError("systemd said no")
        with _mode("always"):
            assert await _after_pairing(reloader, False) == "failed"
        assert reloader.pending is True  # retried on the next poll (#1490)

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-008"])
    async def test_failed_restart_keeps_its_records_and_is_retried_after_a_wait(self):
        """#1490: a failed restart must not drop the records bluetoothd still has to load."""
        now = {"t": 0.0}
        reloader = BluezReloader(
            game_busy=AsyncMock(return_value=False),
            restart=(restart := AsyncMock(side_effect=[RuntimeError("systemd said no"), None])),
            clock=lambda: now["t"],
        )
        with _mode("when_idle"), patch.object(bluez_reload, "write_device_record", return_value=RecordOutcome.EXISTS):
            reloader.request(_record())
            assert await reloader.run_pending() == "failed"
            assert reloader.pending is True
            assert list(reloader.records) == [(ADAPTER, CONTROLLER)]

            now["t"] = 10.0  # next poll, once the first wait has passed
            assert await reloader.run_pending() == "restarted"
        assert restart.await_count == 2
        assert reloader.pending is False
        assert reloader.records == {}
        assert reloader.failures == 0


class TestFailedRestartBackoff:
    """#1490 review: failed restarts are retried after 10 s, 30 s, 60 s, then every 5 min (UC-001 BR-005)."""

    @staticmethod
    def _reloader(restart: AsyncMock) -> tuple[BluezReloader, dict, AsyncMock]:
        now = {"t": 0.0}
        game_busy = AsyncMock(return_value=False)
        reloader = BluezReloader(game_busy=game_busy, restart=restart, clock=lambda: now["t"])
        reloader.pending = True
        return reloader, now, game_busy

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_waits_grow_and_are_capped_at_five_minutes(self):
        restart = AsyncMock(side_effect=RuntimeError("systemd said no"))
        reloader, now, game_busy = self._reloader(restart)
        attempts = []
        with _mode("when_idle"):
            for second in range(0, 1200, 5):  # a poll every 5 s for 20 min
                now["t"] = float(second)
                if await reloader.run_pending() == "failed":
                    attempts.append(second)

        # 0, +10, +30, +60, then every +300
        assert attempts[:6] == [0, 10, 40, 100, 400, 700]
        assert restart.await_count == len(attempts)
        assert game_busy.await_count == len(attempts)  # waiting attempts read no game state
        assert reloader.pending is True

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_success_resets_the_wait(self):
        restart = AsyncMock(side_effect=[RuntimeError("1"), RuntimeError("2"), None, RuntimeError("3")])
        reloader, now, _game_busy = self._reloader(restart)
        with _mode("always"):
            assert await reloader.run_pending() == "failed"  # t=0, wait 10 s
            now["t"] = 10.0
            assert await reloader.run_pending() == "failed"  # wait 30 s
            now["t"] = 39.0
            assert await reloader.run_pending() == "backoff"
            now["t"] = 40.0
            assert await reloader.run_pending() == "restarted"
            assert reloader.failures == 0

            reloader.request()  # a later pairing
            assert await reloader.run_pending() == "failed"  # t=40, back to the first wait
            now["t"] = 49.0
            assert await reloader.run_pending() == "backoff"
            now["t"] = 50.0
            assert await reloader.run_pending() == "failed"

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_first_failure_is_an_error_later_ones_warnings(self, caplog):
        restart = AsyncMock(side_effect=RuntimeError("systemd said no"))
        reloader, now, _game_busy = self._reloader(restart)
        with _mode("always"), caplog.at_level("WARNING", logger="psmove-pairing"):
            for second in (0.0, 10.0, 40.0):
                now["t"] = second
                assert await reloader.run_pending() == "failed"

        levels = [r.levelname for r in caplog.records if "Failed to restart" in r.getMessage()]
        assert levels == ["ERROR", "WARNING", "WARNING"]


class TestOneRestartPerPoll:
    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_three_new_controllers_in_one_poll_cost_one_restart(self):
        reloader, _game_busy, restart = _reloader()
        with _mode("when_idle"):
            for i in range(3):
                assert reloader.request(_record(controller=f"00:06:F7:00:00:0{i}")) == "pending"
            assert await reloader.run_pending() == "restarted"
            assert await reloader.run_pending() == "skipped"
        restart.assert_awaited_once()
        assert reloader.records == {}

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_request_without_a_record_keeps_an_earlier_pending_restart(self):
        reloader, _game_busy, _restart = _reloader(busy=True)
        with _mode("when_idle"):
            reloader.request(_record())
            assert await reloader.run_pending() == "deferred"
            assert reloader.request(None) == "pending"  # a known controller paired meanwhile
        assert reloader.pending is True


class TestPendingRecordsSurviveTheWait:
    """Review of #1476: bluetoothd may delete an unloaded stored record before the deferred restart."""

    @pytest.fixture
    def storage(self, tmp_path: Path) -> Path:
        (tmp_path / ADAPTER).mkdir()
        return tmp_path

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005", "BR-008"])
    async def test_record_deleted_during_the_game_is_written_again_before_the_restart(self, storage):
        from psmove_pairing.bluez_records import write_device_record

        reloader, game_busy, restart = _reloader(busy=True)
        assert write_device_record(ADAPTER, CONTROLLER, 0x03D5, storage) is RecordOutcome.WRITTEN
        info = storage / ADAPTER / CONTROLLER / "info"
        seen_at_restart = []
        restart.side_effect = lambda: seen_at_restart.append(info.is_file())

        with _mode("when_idle"):
            reloader.request(_record(storage))
            assert await reloader.run_pending() == "deferred"
            info.unlink()  # bluetoothd removed the stored record (device_remove_stored)
            info.parent.rmdir()
            game_busy.return_value = False
            assert await reloader.run_pending() == "restarted"

        assert seen_at_restart == [True]
        assert "Trusted=true" in info.read_text()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-008"])
    async def test_existing_record_is_left_alone_before_the_restart(self, storage):
        reloader, _game_busy, _restart = _reloader()
        device = storage / ADAPTER / CONTROLLER
        device.mkdir()
        (device / "info").write_text("[General]\nName=Motion Controller\n\n[LinkKey]\nKey=00\n")
        with _mode("when_idle"):
            reloader.request(_record(storage))
            await reloader.run_pending()
        assert "[LinkKey]" in (device / "info").read_text()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_never_drops_pending_records(self):
        reloader, _game_busy, restart = _reloader(busy=True)
        with _mode("when_idle"):
            reloader.request(_record())
            await reloader.run_pending()
        with _mode("never"):
            assert await reloader.run_pending() == "skipped"
        assert reloader.records == {}
        restart.assert_not_awaited()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(body: dict, seen: list):
    def urlopen(request: urllib.request.Request, timeout: float):  # OpenerDirector.open(request, timeout=...)
        seen.append(request)
        return _Response(json.dumps(body).encode())

    return urlopen


class TestPrimaryGameBusy:
    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize(
        ("game_info", "busy"),
        [
            ({"state": "STARTING"}, True),
            ({"state": "RUNNING"}, True),
            ({"state": "ENDING"}, True),
            ({"state": "ENDED"}, False),
            ({"state": "IDLE"}, False),
            ({}, False),  # proto3 JSON omits the default IDLE
        ],
    )
    async def test_busy_only_while_the_primary_game_is_live(self, game_info, busy):
        seen = []
        with patch.object(bluez_reload._OPENER, "open", _serve({"success": True, "gameInfo": game_info}, seen)):
            assert await primary_game_busy() is busy

        # Empty GetGameStateRequest = the primary (real) game; shadow games are never asked about
        assert [r.data for r in seen] == [b"{}"]
        assert seen[0].full_url == bluez_reload.DEFAULT_GAME_STATE_URL
        assert seen[0].get_method() == "POST"

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_unreachable_coordinator_counts_as_a_running_game(self):
        with patch.object(bluez_reload._OPENER, "open", side_effect=OSError("connection refused")):
            assert await primary_game_busy() is True

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize("body", [{"success": False, "error": "boom"}, {"success": False}, {}])
    async def test_unsuccessful_answer_counts_as_a_running_game(self, body):
        """{"success": false} (with or without an error, or no flag at all) is not a state: fail closed."""
        with patch.object(bluez_reload._OPENER, "open", _serve(body, [])):
            assert await primary_game_busy() is True

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_unsuccessful_answer_with_an_idle_state_still_counts_as_running(self):
        body = {"success": False, "error": "boom", "gameInfo": {"state": "IDLE"}}
        with patch.object(bluez_reload._OPENER, "open", _serve(body, [])):
            assert await primary_game_busy() is True

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_url_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GAME_STATE_URL", "http://envoy/joustmania.x/GetGameState")
        seen = []
        with patch.object(bluez_reload._OPENER, "open", _serve({"success": True}, seen)):
            await primary_game_busy()
        assert seen[0].full_url == "http://envoy/joustmania.x/GetGameState"


class _GameStateHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        body = json.dumps({"success": True, "gameInfo": {"state": "ENDED"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class TestGameStateReadIgnoresProxies:
    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_http_proxy_in_the_environment_is_not_used(self, monkeypatch):
        """A host HTTP_PROXY must not intercept the localhost read (else: always busy, never restarted)."""
        server = http.server.HTTPServer(("127.0.0.1", 0), _GameStateHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
                monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens on the discard port
            for name in ("NO_PROXY", "no_proxy"):
                monkeypatch.delenv(name, raising=False)
            monkeypatch.setenv("GAME_STATE_URL", f"http://127.0.0.1:{server.server_port}/GetGameState")

            assert await primary_game_busy() is False
        finally:
            server.shutdown()
            server.server_close()


class TestRestartBluetoothd:
    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_restart_registers_the_pairing_agent_again(self):
        """A bluetoothd restart drops BlueZ's agent registration (#632); it is registered again after it."""
        calls = []
        with (
            patch.object(
                bluez_reload.bluez_dbus, "restart_systemd_unit", AsyncMock(side_effect=lambda u: calls.append(u))
            ),
            patch.object(bluez_reload, "register_pairing_agent", AsyncMock(side_effect=lambda: calls.append("agent"))),
            patch("asyncio.sleep", AsyncMock()),
        ):
            await bluez_reload.restart_bluetoothd()

        assert calls == ["bluetooth.service", "agent"]

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_agent_is_kept_when_systemd_refuses_the_restart(self):
        """RestartUnit returned an error: no job, bluetoothd still runs with its agent; a new one would open a gap."""
        agent = AsyncMock()
        with (
            patch.object(bluez_reload.bluez_dbus, "restart_systemd_unit", AsyncMock(side_effect=RuntimeError("no"))),
            patch.object(bluez_reload, "register_pairing_agent", agent),
            patch("asyncio.sleep", AsyncMock()),
            pytest.raises(RuntimeError),
        ):
            await bluez_reload.restart_bluetoothd()

        agent.assert_not_awaited()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_agent_is_registered_when_the_settle_wait_is_cut_short(self):
        """systemd accepted the restart, so the old session is gone: register the agent even on cancellation."""
        agent = AsyncMock()
        with (
            patch.object(bluez_reload.bluez_dbus, "restart_systemd_unit", AsyncMock()),
            patch.object(bluez_reload, "register_pairing_agent", agent),
            patch("asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError)),
            pytest.raises(asyncio.CancelledError),
        ):
            await bluez_reload.restart_bluetoothd()

        agent.assert_awaited_once()


class TestUsbQuietPeriod2091:
    """#2091: with when_idle a pending restart waits until no PS Move was on USB for the quiet period."""

    @staticmethod
    def _quiet(seconds: float):
        return patch("psmove_pairing.bluez_reload.get_restart_quiet_period", return_value=seconds)

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_waits_for_quiet_usb_without_asking_the_game(self):
        reloader, game_busy, restart = _reloader()
        now = {"t": 100.0}
        reloader._clock = lambda: now["t"]
        with _mode("when_idle"), self._quiet(20):
            reloader.note_usb_controllers(1)
            assert await _after_pairing(reloader, True) == "waiting_usb"
            now["t"] = 119.0
            reloader.note_usb_controllers(0)  # an empty listing does not restart the quiet period
            assert await reloader.run_pending() == "waiting_usb"
            game_busy.assert_not_awaited()
            now["t"] = 120.0
            assert await reloader.run_pending() == "restarted"
        restart.assert_awaited_once()

    @pytest.mark.uc("UC-001", flow="A7", br=["BR-005"])
    async def test_nothing_seen_on_usb_restarts_at_once(self):
        reloader, _game_busy, restart = _reloader()
        with _mode("when_idle"), self._quiet(20):
            assert await _after_pairing(reloader, True) == "restarted"
        restart.assert_awaited_once()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_always_is_not_batched(self):
        reloader, _game_busy, restart = _reloader()
        with _mode("always"), self._quiet(20):
            reloader.note_usb_controllers(3)
            assert await _after_pairing(reloader, True) == "restarted"
        restart.assert_awaited_once()
