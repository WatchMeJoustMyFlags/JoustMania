"""Tests for the menu server boot path (services/menu/server.py)."""

import asyncio
import contextlib
import signal
from unittest.mock import create_autospec, patch

import grpc
import pytest
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


async def _boot(servicer) -> None:
    """Run server.serve() until the boot sequence is done, then shut it down via SIGTERM."""
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
    ):
        task = asyncio.create_task(server.serve(port=0))
        for _ in range(200):
            if signal.SIGTERM in handlers:
                break
            await asyncio.sleep(0.01)
        handlers[signal.SIGTERM]()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)


class TestMenuAutoStart:
    """UC-010 BR-003: the menu_auto_start boot path."""

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", br=["BR-003"])
    async def test_auto_start_runs_lobby_without_music_or_idle_monitor(self):
        """menu_auto_start on (the default): RUNNING with JoustFFA, no StartMenu side effects."""
        servicer = _servicer_with_flags()  # flag unset -> default True

        await _boot(servicer)

        assert servicer.state == menu_pb2.MenuState.RUNNING
        # The persisted current_game is ignored on this path (D-M2).
        assert servicer.current_selection == Games.JoustFFA
        servicer.StartMenu.assert_not_called()
        servicer.start_button_monitor.assert_awaited_once()
        # No lobby music and no idle monitor until the first game ends.
        assert not servicer.audio.mock_calls
        assert not servicer.idle_monitor.mock_calls

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", br=["BR-003"])
    async def test_auto_start_off_leaves_menu_stopped(self):
        """menu_auto_start off: the menu stays STOPPED until StartMenu is called."""
        servicer = _servicer_with_flags(menu_auto_start=False)

        await _boot(servicer)

        assert servicer.state == menu_pb2.MenuState.STOPPED
        assert servicer.current_selection == Games.Tournament

    @pytest.mark.asyncio
    async def test_shutdown_stops_monitors_and_server(self):
        """SIGTERM stops the button monitor and lifecycle, shuts the servicer down and stops the server."""
        servicer = _servicer_with_flags()

        await _boot(servicer)

        servicer.stop_button_monitor.assert_awaited_once()
        servicer.stop_game_event_monitor.assert_awaited_once()
        servicer.shutdown.assert_awaited_once()
