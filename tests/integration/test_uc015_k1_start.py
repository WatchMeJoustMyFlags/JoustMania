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

        known = {e.game_id for e in collector.get_events("game_starting")}
        await mark_controllers_ready(mock_client, serials[:2])
        await asyncio.sleep(0.5)
        assert {e.game_id for e in collector.get_events("game_starting")} == known, (
            "two of four ready must not auto start"
        )

        # Step 2: the K1 tap, as the pad service sends it.
        reply = await menu_client.ProcessInput(
            menu_pb2.ProcessInputRequest(input_type="web_command", data={"command": "start_game"})
        )
        assert reply.success, f"start_game refused: {reply.error}"

        # Step 3: the usual countdown, then the game runs.
        event = await collector.wait_for_any_event(["game_starting", "game_started"], timeout=20.0)
        assert event.game_id not in known
        running = await wait_until_running(game_client, {event.game_id}, timeout=30.0)
        assert event.game_id in running
    finally:
        await collector.stop()
