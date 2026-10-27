"""Tests for the menu server boot path (services/menu/server.py)."""

import asyncio
import contextlib
import signal
from unittest.mock import create_autospec, patch

import grpc
import pytest
from grpc_health.v1 import health, health_pb2
from openfeature.client import OpenFeatureClient

from lib.types import Games
from proto import menu_pb2
from services.menu import server
from services.menu.idle_monitor import IdleMonitor
from services.menu.servicer import MenuServicer
from services.menu.utils import AudioHelper


def _servicer_with_flags(**flags):
    """A MenuServicer autospec whose user-prefs client resolves the given flags (else the default)."""
    servicer = create_autospec(MenuServicer, instance=True)
    servicer.state = menu_pb2.MenuState.STOPPED
    servicer.current_selection = Games.Tournament
    servicer.audio = create_autospec(AudioHelper, instance=True)
    servicer.idle_monitor = create_autospec(IdleMonitor, instance=True)
    servicer.user_prefs_client = create_autospec(OpenFeatureClient, instance=True)
    servicer.user_prefs_client.get_boolean_value.side_effect = lambda key, default, *_a, **_k: flags.get(key, default)
    return servicer


async def _provider_ready(_domain, *_a, **_k):
    return True


async def _boot(servicer, provider_wait=None):
    """Run server.serve() until the boot sequence is done, then SIGTERM it; returns the gRPC server mock."""
    loop = asyncio.get_running_loop()
    handlers = {}
    grpc_server = create_autospec(grpc.aio.Server, instance=True)
    with (
        patch.object(server, "init_metrics", autospec=True),
        patch.object(server, "init_logging", autospec=True),
        patch.object(
            server, "start_system_metrics_collector", autospec=True, return_value=asyncio.create_task(asyncio.sleep(10))
        ),
        patch.object(server.grpc.aio, "server", autospec=True, return_value=grpc_server),
        patch.object(server, "MenuServicer", autospec=True, return_value=servicer),
        patch.object(server.menu_pb2_grpc, "add_MenuServiceServicer_to_server", autospec=True),
        patch.object(loop, "add_signal_handler", side_effect=lambda sig, cb: handlers.__setitem__(sig, cb)),
        patch.object(server, "wait_for_provider_ready", side_effect=provider_wait or _provider_ready),
    ):
        task = asyncio.create_task(server.serve(port=0))
        for _ in range(200):
            if signal.SIGTERM in handlers:
                break
            await asyncio.sleep(0.01)
        handlers[signal.SIGTERM]()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    return grpc_server


class TestMenuAutoStart:
    """UC-010 BR-003: the menu_auto_start boot path."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", br=["BR-003"])
    async def test_auto_start_enters_lobby_like_start_menu(self, real_servicer):
        """menu_auto_start on (the default) takes the StartMenu lobby entry (D-M2, #1505, #1285).

        The persisted current_game is restored, lobby music and the idle monitor
        start and menu_started is published, exactly as after StartMenu.
        """
        real_servicer.user_prefs_client.get_string_value.side_effect = lambda key, default, *_a, **_k: (
            "Tournament" if key == "current_game" else default
        )

        await _boot(real_servicer)

        assert real_servicer.state == menu_pb2.MenuState.RUNNING
        assert real_servicer.current_selection == Games.Tournament
        assert real_servicer.state_manager.current_game_mode == Games.Tournament
        real_servicer.audio.start_lobby_music.assert_awaited_once()
        real_servicer.idle_monitor.start.assert_called_once()
        published = [c.args for c in real_servicer.event_publisher.publish.await_args_list]
        assert ("menu_started", {}) in published

    @pytest.mark.asyncio
    async def test_auto_start_ignores_unplayable_persisted_selection(self, real_servicer):
        """A persisted current_game that is not playable falls back to JoustFFA on boot."""
        real_servicer.user_prefs_client.get_string_value.side_effect = lambda key, default, *_a, **_k: (
            "Random" if key == "current_game" else default
        )
        real_servicer.current_selection = Games.Tournament

        await _boot(real_servicer)

        assert real_servicer.current_selection == Games.JoustFFA
        assert real_servicer.state_manager.current_game_mode == Games.JoustFFA

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", br=["BR-003"])
    async def test_auto_start_reads_user_flags_after_the_provider_synced(self, real_servicer):
        """The boot waits for the user flag provider before it reads menu_auto_start and current_game."""
        order = []

        async def provider_wait(domain, *_a, **_k):
            order.append(f"wait {domain}")
            return True

        real_servicer.user_prefs_client.get_boolean_value.side_effect = lambda key, default, *_a, **_k: (
            order.append(f"read {key}") or default
        )

        await _boot(real_servicer, provider_wait=provider_wait)

        assert order[:2] == ["wait user", "read menu_auto_start"]
        assert real_servicer.state == menu_pb2.MenuState.RUNNING

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", br=["BR-003"])
    async def test_start_menu_during_provider_wait_is_not_re_entered(self, real_servicer):
        """StartMenu while the boot waits for the provider: the auto-start must not enter the lobby again."""
        real_servicer.user_prefs_client.get_string_value.side_effect = lambda key, default, *_a, **_k: (
            "Tournament" if key == "current_game" else default
        )

        async def provider_wait(_domain, *_a, **_k):
            await real_servicer.StartMenu(menu_pb2.StartMenuRequest(), None)
            await real_servicer.select_game_mode("Tournament", source="web")
            real_servicer.current_selection = Games.Tournament
            real_servicer.user_prefs_client.get_string_value.side_effect = lambda _key, default, *_a, **_k: default
            return True

        await _boot(real_servicer, provider_wait=provider_wait)

        real_servicer.audio.start_lobby_music.assert_awaited_once()
        published = [c.args[0] for c in real_servicer.event_publisher.publish.await_args_list]
        assert published.count("menu_started") == 1
        assert real_servicer.current_selection == Games.Tournament

    @pytest.mark.asyncio
    async def test_provider_timeout_logs_warning(self, real_servicer, caplog):
        async def not_ready(_domain, *_a, **_k):
            return False

        await _boot(real_servicer, provider_wait=not_ready)

        assert "flag provider not ready after 5s" in caplog.text

    @pytest.mark.asyncio
    async def test_auto_start_off_leaves_menu_stopped(self):
        """menu_auto_start off: the menu stays STOPPED until StartMenu is called."""
        servicer = _servicer_with_flags(menu_auto_start=False)

        await _boot(servicer)

        assert servicer.state == menu_pb2.MenuState.STOPPED
        assert servicer.current_selection == Games.Tournament

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-002", cond=["Pre-3"])
    async def test_boot_subscribes_to_controllers_and_runs_the_lobby(self):
        """UC-002 Pre-3: a booted menu listens on the button stream and runs the lobby.

        The button monitor (the controller manager's connect events) starts after the
        gRPC server and before the lobby entry, so a controller that connects lands in
        a running lobby.
        """
        servicer = _servicer_with_flags()
        calls = []
        servicer.start_button_monitor.side_effect = lambda: calls.append("start_button_monitor")
        servicer.enter_lobby.side_effect = lambda: calls.append("enter_lobby")

        grpc_server = await _boot(servicer)

        grpc_server.start.assert_awaited_once()
        assert calls == ["start_button_monitor", "enter_lobby"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-015", flow="main", steps=[9], br=["BR-018"])
    async def test_boot_reads_the_operator_shield_mode_once(self):
        """UC-015 BR-018: the first K6 hold after a restart starts from a value read at boot."""
        servicer = _servicer_with_flags()

        await _boot(servicer)

        servicer.read_shield_operator_at_start.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_shutdown_stops_monitors_and_server(self):
        """SIGTERM stops the button monitor and lifecycle, shuts the servicer down and stops the server."""
        servicer = _servicer_with_flags()

        grpc_server = await _boot(servicer)

        servicer.stop_button_monitor.assert_awaited_once()
        servicer.stop_game_event_monitor.assert_awaited_once()
        servicer.shutdown.assert_awaited_once()
        grpc_server.start.assert_awaited_once()
        grpc_server.stop.assert_awaited_once_with(grace=5)


class TestMenuHealthReadiness:
    """UC-010 Pre-1 (#1826): health is SERVING only after the boot lobby entry."""

    @staticmethod
    def _recording_health(events):
        class Recording(health.aio.HealthServicer):
            async def set(self, service, status):
                events.append(("health", service, status))
                await super().set(service, status)

        return Recording

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", cond=["Pre-1"])
    async def test_health_not_serving_until_boot_lobby_entry_completes(self):
        events = []
        servicer = _servicer_with_flags()

        async def slow_enter_lobby():
            await asyncio.sleep(0.2)
            events.append("lobby_entered")

        servicer.enter_lobby.side_effect = slow_enter_lobby

        with patch.object(server.health.aio, "HealthServicer", self._recording_health(events)):
            await _boot(servicer)

        serving = health_pb2.HealthCheckResponse.SERVING
        assert events.index("lobby_entered") < events.index(("health", "", serving))
        assert events.index("lobby_entered") < events.index(("health", "menu.MenuService", serving))
        assert not any(e[2] == serving for e in events[: events.index("lobby_entered")] if isinstance(e, tuple))

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", cond=["Pre-1"])
    async def test_health_serving_without_lobby_entry_when_auto_start_off(self):
        events = []
        servicer = _servicer_with_flags(menu_auto_start=False)

        with patch.object(server.health.aio, "HealthServicer", self._recording_health(events)):
            await _boot(servicer)

        servicer.enter_lobby.assert_not_called()
        assert ("health", "", health_pb2.HealthCheckResponse.SERVING) in events

    @pytest.mark.asyncio
    async def test_health_serving_after_provider_timeout(self):
        events = []

        async def not_ready(_domain, *_a, **_k):
            return False

        with patch.object(server.health.aio, "HealthServicer", self._recording_health(events)):
            await _boot(_servicer_with_flags(), provider_wait=not_ready)

        assert ("health", "", health_pb2.HealthCheckResponse.SERVING) in events

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", cond=["Pre-1"])
    async def test_hung_boot_lobby_entry_still_reports_serving_after_the_timeout(self):
        events = []
        servicer = _servicer_with_flags()

        async def hung_enter_lobby():
            await asyncio.sleep(3600)

        servicer.enter_lobby.side_effect = hung_enter_lobby

        with (
            patch.object(server, "BOOT_LOBBY_ENTRY_TIMEOUT_SECONDS", 0.05),
            patch.object(server.health.aio, "HealthServicer", self._recording_health(events)),
        ):
            await _boot(servicer)

        assert ("health", "", health_pb2.HealthCheckResponse.SERVING) in events

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", cond=["Pre-1"])
    async def test_raising_boot_lobby_entry_still_reports_serving(self):
        events = []
        servicer = _servicer_with_flags()
        servicer.enter_lobby.side_effect = RuntimeError("audio down")

        with patch.object(server.health.aio, "HealthServicer", self._recording_health(events)):
            await _boot(servicer)

        assert ("health", "", health_pb2.HealthCheckResponse.SERVING) in events
