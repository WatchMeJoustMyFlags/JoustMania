"""Every selection path repaints the lobby in the new mode's colour (#2105, UC-011 step 6, BR-005).

A real MenuServicer (servicer_harness): admin Cross, lobby SELECT, the web ``select_game`` and
``start_game`` with a mode, and the operator pad all go through ``MenuServicer.select_game_mode``.
"""

from unittest.mock import patch

import pytest
import pytest_asyncio

from proto import menu_pb2
from services.menu.handlers.base import ControllerState
from services.menu.tests.servicer_harness import cancel_background_tasks, make_real_servicer


@pytest_asyncio.fixture
async def servicer():
    menu = make_real_servicer()
    menu.state = menu_pb2.MenuState.RUNNING
    menu.state_manager.controller_states = {
        "dim": ControllerState.CONNECTED,
        "ready": ControllerState.READY,
        "admin": ControllerState.ADMIN,
        "idle": ControllerState.IDLE,
    }
    yield menu
    await cancel_background_tasks(menu)


def web(command: str, **data) -> menu_pb2.ProcessInputRequest:
    return menu_pb2.ProcessInputRequest(input_type="web_command", data={"command": command, **data})


def assert_lobby_repainted(menu) -> None:
    """Dim for CONNECTED, bright for READY, in the selected mode; ADMIN and IDLE untouched."""
    mode = menu.current_selection.name
    assert mode != "JoustFFA"  # the selection moved off the default
    led = menu.state_manager.led
    assert [c.args for c in led.set_connected_color.await_args_list] == [("dim", mode)]
    assert [c.args for c in led.set_ready_color.await_args_list] == [("ready", mode)]


@pytest.mark.asyncio
@pytest.mark.uc("UC-011", flow="main", steps=[6], br=["BR-005"])
async def test_admin_cross_repaints_the_lobby(servicer):
    await servicer.admin_handler.handle_game_mode("admin")
    assert_lobby_repainted(servicer)


@pytest.mark.asyncio
@pytest.mark.uc("UC-011", flow="A2", br=["BR-005"])
async def test_lobby_select_repaints_the_lobby(servicer):
    await servicer.on_button("dim", "select", True)
    assert_lobby_repainted(servicer)


@pytest.mark.asyncio
@pytest.mark.uc("UC-011", flow="A1", br=["BR-005"])
async def test_web_select_game_repaints_the_lobby(servicer):
    await servicer.ProcessInput(web("select_game", game_name="Swapper"), None)
    assert_lobby_repainted(servicer)


@pytest.mark.asyncio
@pytest.mark.uc("UC-011", br=["BR-002", "BR-005"])
async def test_web_start_game_with_a_mode_repaints_the_lobby(servicer):
    with patch.object(servicer, "_start_game", autospec=True):
        await servicer.ProcessInput(web("start_game", mode="Swapper"), None)
    assert_lobby_repainted(servicer)


@pytest.mark.asyncio
@pytest.mark.uc("UC-011", flow="A4", br=["BR-005"])
@pytest.mark.uc("UC-015", flow="main", steps=[4], br=["BR-015"])
async def test_pad_mode_step_repaints_the_lobby(servicer):
    await servicer.ProcessInput(web("step_setting", option="current_game", direction="up", steps="1"), None)
    assert_lobby_repainted(servicer)


@pytest.mark.asyncio
@pytest.mark.uc("UC-011", br=["BR-005"])
async def test_a_refused_selection_repaints_nothing(servicer):
    await servicer.ProcessInput(web("select_game", game_name="Commander"), None)
    servicer.state_manager.led.set_connected_color.assert_not_awaited()
    servicer.state_manager.led.set_ready_color.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.uc("UC-011", flow="A2", br=["BR-005"])
async def test_the_state_manager_fallback_repaints_like_the_servicer(servicer):
    """The callback-less StateManager path (kept in step with select_game_mode) repaints too."""
    from lib.types import Games

    manager = servicer.state_manager
    manager._select_game_mode = None
    await manager.select_game_mode(Games.Swapper, source="controller")
    servicer.current_selection = manager.current_game_mode
    assert_lobby_repainted(servicer)
