"""UC-015 main steps 2 and 3: the pad's K1 tap starts a game through the menu (#2050 PR8).

The pad service sends `ProcessInput` `start_game` for a K1 tap (UC-015 step 2); these tests send
that exact command to the real menu on mock controllers and check step 3: the game starts as in
UC-010 step 5 onwards, announced by the countdown (`game_starting`, then RUNNING).

The menu keeps its controller roster across StopMenu / StartMenu since #2175; the negative test
checks that two of four ready then do not auto start.
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import controller_manager_mock_pb2, menu_pb2
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    get_game_client,
    get_menu_client,
    get_mock_client,
    mark_controllers_ready,
    select_game_mode,
    setup_mock_controllers,
    wait_until_running,
)

CONTROLLERS = 4
SETTLE_SECONDS = 1.5  # longer than the ready -> auto start delay seen in CI (about 0.4 s)


async def _status(menu_client):
    return await menu_client.GetMenuStatus(menu_pb2.GetMenuStatusRequest())


async def _register_idle(mock_client, serials):
    """A controller is known to the menu once it sends a button event: MOVE neither readies nor starts."""
    for serial in serials:
        for pressed in (True, False):
            await mock_client.SimulateButton(
                controller_manager_mock_pb2.ButtonRequest(
                    serial=serial, button=controller_manager_mock_pb2.ButtonRequest.Button.MOVE, pressed=pressed
                )
            )
            await asyncio.sleep(0.05)


async def _wait_connected(menu_client, count, timeout=10.0) -> bool:
    async def _all_known() -> bool:
        return (await _status(menu_client)).connected_count == count

    return await async_poll_until(_all_known, timeout=timeout)


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[2, 3])
async def test_uc015_k1_start_game_reaches_the_countdown(docker_compose, ensure_game_stopped):
    """Two of four known controllers ready, then the pad's `start_game`: generation up, countdown, RUNNING."""
    mock_client, _ = await get_mock_client(docker_compose)
    game_client, _ = await get_game_client(docker_compose)
    menu_client, _ = await get_menu_client(docker_compose)

    collector = GameEventCollector(game_client)
    try:
        await collector.start()
        serials = await setup_mock_controllers(docker_compose, count=CONTROLLERS)
        assert len(serials) == CONTROLLERS

        await _register_idle(mock_client, serials)
        assert await select_game_mode(menu_client, "JoustFFA"), "select_game rejected"
        assert await _wait_connected(menu_client, CONTROLLERS), "the menu does not know all four controllers"

        await mark_controllers_ready(mock_client, serials[:2])
        assert await async_poll_until(lambda: _ready_is_two(menu_client), timeout=10.0), "two controllers not ready"

        # Positive signals only: the pad's start must be what raises the generation.
        before = await _status(menu_client)
        assert not before.lifecycle_live, "a game is live before the pad's start (auto start fired)"
        known = {e.game_id for e in collector.get_events() if e.game_id}

        # Step 2: the K1 tap, as the pad service sends it.
        reply = await menu_client.ProcessInput(
            menu_pb2.ProcessInputRequest(input_type="web_command", data={"command": "start_game"})
        )
        assert reply.success

        # Step 3: a new lifecycle, the usual countdown, then the game runs.
        async def _generation_up() -> bool:
            return (await _status(menu_client)).lifecycle_generation > before.lifecycle_generation

        assert await async_poll_until(_generation_up, timeout=20.0), "start_game did not start a new lifecycle"
        new_ids: set[str] = set()

        async def _new_game_starting() -> bool:
            new_ids.update(
                e.game_id
                for e in collector.get_events("game_starting")
                if e.game_id and e.game_id not in known
            )
            return bool(new_ids)

        assert await async_poll_until(_new_game_starting, timeout=20.0), "no new game_starting"
        running = await wait_until_running(game_client, new_ids, timeout=30.0)
        assert new_ids <= running
    finally:
        await collector.stop()


async def _ready_is_two(menu_client) -> bool:
    return (await _status(menu_client)).ready_count == 2


@pytest.mark.asyncio
@pytest.mark.uc("UC-010", flow="main", steps=[4])
async def test_uc015_two_of_four_ready_after_a_menu_restart_does_not_auto_start(docker_compose, ensure_game_stopped):
    """UC-010 step 4: auto start needs every connected controller ready, also after StopMenu / StartMenu."""
    mock_client, _ = await get_mock_client(docker_compose)
    menu_client, _ = await get_menu_client(docker_compose)

    serials = await setup_mock_controllers(docker_compose, count=CONTROLLERS)
    await menu_client.StopMenu(menu_pb2.StopMenuRequest())
    started = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
    assert started.success, f"StartMenu failed: {started.error}"

    # Not fatal: a menu that lost controllers (#2175) is reported by the asserts below.
    await _wait_connected(menu_client, CONTROLLERS, timeout=3.0)
    await mark_controllers_ready(mock_client, serials[:2])
    await asyncio.sleep(SETTLE_SECONDS)

    status = await _status(menu_client)
    assert status.connected_count == CONTROLLERS
    assert status.ready_count == 2
    assert not status.lifecycle_live
