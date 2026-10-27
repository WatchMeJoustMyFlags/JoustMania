"""UC-015 main step 3: the pad's K1 tap starts a game through the menu (#2050 PR8).

The pad service sends `ProcessInput` `start_game` for a K1 tap (UC-015 step 2); this test sends
that exact command to the real menu on mock controllers and checks step 3: the game starts as in
UC-010 step 5 onwards, announced by the countdown (`game_starting`, then RUNNING).
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import menu_pb2
from tests.integration.helpers import (
    GameEventCollector,
    get_game_client,
    get_menu_client,
    get_mock_client,
    mark_controllers_ready,
    select_game_mode,
    setup_mock_controllers,
    wait_until_running,
)


@pytest.mark.asyncio
@pytest.mark.uc("UC-015", flow="main", steps=[2, 3])
async def test_uc015_k1_start_game_reaches_the_countdown(docker_compose, ensure_game_stopped):
    """Two of four controllers ready (no auto start), then the pad's `start_game`: countdown, then RUNNING."""
    mock_client, _mock_channel = await get_mock_client(docker_compose)
    game_client, _game_channel = await get_game_client(docker_compose)
    menu_client, _menu_channel = await get_menu_client(docker_compose)

    collector = GameEventCollector(game_client)
    try:
        await collector.start()
        serials = await setup_mock_controllers(docker_compose, count=4)
        assert len(serials) == 4

        await menu_client.StopMenu(menu_pb2.StopMenuRequest())
        started_menu = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
        assert started_menu.success, f"StartMenu failed: {started_menu.error}"
        assert await select_game_mode(menu_client, "JoustFFA"), "select_game rejected"

        # Auto start needs every connected controller ready (UC-010 step 4), so with two of the four
        # ready the lobby waits. No negative wait: ids seen so far (also from earlier tests) are excluded.
        await mark_controllers_ready(mock_client, serials[:2])
        await asyncio.sleep(0.5)
        known = {e.game_id for e in collector.get_events() if e.game_id}

        # Step 2: the K1 tap, as the pad service sends it.
        reply = await menu_client.ProcessInput(
            menu_pb2.ProcessInputRequest(input_type="web_command", data={"command": "start_game"})
        )
        assert reply.success, f"start_game refused: {reply.error}"

        # Step 3: the usual countdown, then the game runs.
        deadline = asyncio.get_running_loop().time() + 20.0
        new_ids: set[str] = set()
        while not new_ids and asyncio.get_running_loop().time() < deadline:
            new_ids = {
                e.game_id
                for e in collector.get_events()
                if e.event_type in ("game_starting", "game_started") and e.game_id not in known
            }
            await asyncio.sleep(0.1)
        assert new_ids, "no new game_starting after the pad's start_game"
        running = await wait_until_running(game_client, new_ids, timeout=30.0)
        assert new_ids <= running
    finally:
        await collector.stop()
