"""The event collectors are subscribed when ``start()`` returns (#1613).

``MenuEventCollector`` and ``GameEventCollector`` await the stream's headers,
which the menu and the coordinator send right after the subscriber joins the
event bus. An action taken the moment the ``async with`` block is entered must
therefore show up in the collector: before #1613 ``start()`` returned before the
stream was open and a loaded runner lost the event (TC-005, merge queue of #1601).
The servers' side of the contract is pinned by the in-process tests
``test_headers_mark_the_subscription`` in the menu and coordinator suites.
"""

import pytest

from tests.integration.helpers import (
    GameEventCollector,
    MenuEventCollector,
    build_start_config,
    force_end_game_by_id,
    get_game_client,
    get_menu_client,
    select_game_mode,
    setup_mock_controllers,
    start_game_headless,
)
from tests.service.harness import LOBBY

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = LOBBY

EVENT_TIMEOUT_SECONDS = 10.0


async def test_menu_collector_sees_an_action_taken_on_entry(service_stack):
    """The web select publishes selection_changed before ProcessInput returns.

    Without #1613 the event was lost in 4 of 5 runs.
    """
    menu_client, menu_channel = await get_menu_client(service_stack)
    try:
        async with MenuEventCollector(menu_client) as collector:
            assert await select_game_mode(menu_client, "JoustTeams")
            event = await collector.wait_for_event("selection_changed", timeout=EVENT_TIMEOUT_SECONDS)
        assert event.data["game_name"] == "JoustTeams"
    finally:
        await select_game_mode(menu_client, "JoustFFA")
        await menu_channel.close()


async def test_game_collector_sees_an_action_taken_on_entry(service_stack, headless_cleanup):
    """Smoke test: the force-end event arrives late enough to pass even without #1613.

    The deterministic proof for the coordinator is the in-process
    ``test_headers_mark_the_subscription``; this one checks the collector wiring
    (start() over a real stream, game_id filter) end to end.
    """
    game_client, game_channel = await get_game_client(service_stack)
    tag = headless_cleanup.add_tag("collector-1613")
    serials = await setup_mock_controllers(service_stack, count=2, reserved=True, tag=tag)
    game_id, starter = await start_game_headless(game_client, build_start_config("JoustFFA", serials))
    headless_cleanup.add_game(game_id)
    try:
        async with GameEventCollector(game_client, game_id=game_id) as collector:
            assert await force_end_game_by_id(game_client, game_id, reason="collector-1613")
            event = await collector.wait_for_any_event(
                ["game_force_ended", "game_ended"], timeout=EVENT_TIMEOUT_SECONDS
            )
        assert event.game_id == game_id
    finally:
        await starter.stop()
        await game_channel.close()
