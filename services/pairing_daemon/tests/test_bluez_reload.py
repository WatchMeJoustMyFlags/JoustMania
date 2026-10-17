"""pairing.bluez_restart_mode policy and the primary game-state read (UC-001 BR-005, A6; #1446)."""

import http.server
import io
import json
import threading
import urllib.request
from unittest.mock import AsyncMock, patch

import pytest

from psmove_pairing import bluez_reload
from psmove_pairing.bluez_reload import BluezReloader, primary_game_busy

pytestmark = pytest.mark.asyncio


def _reloader(busy: bool = False) -> tuple[BluezReloader, AsyncMock, AsyncMock]:
    game_busy = AsyncMock(return_value=busy)
    restart = AsyncMock(return_value=None)
    return BluezReloader(game_busy=game_busy, restart=restart), game_busy, restart


def _mode(mode: str):
    return patch("psmove_pairing.bluez_reload.get_bluez_restart_mode", return_value=mode)


class TestBluezReloader:
    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize("record_written", [False, True])
    async def test_never_restarts_nothing(self, record_written):
        reloader, game_busy, restart = _reloader()
        with _mode("never"):
            assert await reloader.after_pairing(record_written) == "skipped"
        restart.assert_not_awaited()
        game_busy.assert_not_awaited()
        assert reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    @pytest.mark.parametrize("record_written", [False, True])
    async def test_always_restarts_even_during_a_game(self, record_written):
        reloader, game_busy, restart = _reloader(busy=True)
        with _mode("always"):
            assert await reloader.after_pairing(record_written) == "restarted"
        restart.assert_awaited_once()
        game_busy.assert_not_awaited()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_when_idle_needs_a_new_device_record(self):
        reloader, game_busy, restart = _reloader()
        with _mode("when_idle"):
            assert await reloader.after_pairing(record_written=False) == "skipped"
        restart.assert_not_awaited()
        game_busy.assert_not_awaited()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_when_idle_restarts_when_no_real_game_runs(self):
        reloader, _game_busy, restart = _reloader(busy=False)
        with _mode("when_idle"):
            assert await reloader.after_pairing(record_written=True) == "restarted"
        restart.assert_awaited_once()
        assert reloader.pending is False

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_when_idle_defers_during_a_real_game_and_runs_after_it(self):
        reloader, game_busy, restart = _reloader(busy=True)
        with _mode("when_idle"):
            assert await reloader.after_pairing(record_written=True) == "deferred"
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
            await reloader.after_pairing(record_written=True)
        with _mode("never"):
            assert await reloader.run_pending() == "skipped"
        assert reloader.pending is False
        restart.assert_not_awaited()

    @pytest.mark.uc("UC-001", flow="A6", br=["BR-005"])
    async def test_failed_restart_is_logged_not_raised(self):
        reloader, _game_busy, restart = _reloader()
        restart.side_effect = RuntimeError("systemd said no")
        with _mode("always"):
            assert await reloader.after_pairing(record_written=False) == "failed"
        assert reloader.pending is False


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
        """A bluetoothd restart ends the bluetoothctl agent session (#632); it is registered again after it."""
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
    async def test_agent_is_registered_even_when_the_restart_fails(self):
        agent = AsyncMock()
        with (
            patch.object(bluez_reload.bluez_dbus, "restart_systemd_unit", AsyncMock(side_effect=RuntimeError("no"))),
            patch.object(bluez_reload, "register_pairing_agent", agent),
            patch("asyncio.sleep", AsyncMock()),
            pytest.raises(RuntimeError),
        ):
            await bluez_reload.restart_bluetoothd()

        agent.assert_awaited_once()
