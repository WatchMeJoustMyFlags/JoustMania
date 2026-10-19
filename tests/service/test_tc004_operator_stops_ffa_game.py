"""TC-004 on the service tier (T2): the operator force-ends an FFA game started from the lobby.

Real controller manager, game coordinator and menu from source (LOBBY), four
unreserved mock controllers readied through the menu. ``ForceEndGame`` is the
call the dashboard's Stop makes (the dashboard path itself, UC-010 A6, is not
exercised):

- an unknown game id fails and the game keeps running (UC-027 A1);
- Stop by id ends the game with its reason, no winner (UC-020 A4);
- Stop with no game fails with ``No game in progress`` (UC-027 A2);
- the menu restores the lobby and a new ready-up starts a new game id.
"""

import pytest

from lib.colors import Colors
from proto import game_coordinator_pb2
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    force_end_game_by_id,
    get_game_client,
    get_mock_client,
    list_games,
    wait_for_lobby_colors,
)
from tests.service.harness import LOBBY
from tests.service.lobby_flow import ready_up_until_started, start_lobby, started_ids, wait_running

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = LOBBY

REASON = "tc004"
EVENT_TIMEOUT_SECONDS = 15.0
# The menu lights CONNECTED controllers in the selected mode's colour at 30 % (menu/utils/led.py).
FFA_LOBBY_COLOR = tuple(int(c * 0.3) for c in Colors.Orange.value)


def _events(collector: GameEventCollector, game_id: str, event_type: str) -> list:
    return [e for e in collector.get_events(event_type) if e.game_id == game_id]


@pytest.mark.uc("UC-020", flow="A4", tc="TC-004")
@pytest.mark.uc("UC-027", flow="main", tc="TC-004")
@pytest.mark.uc("UC-027", flow="A1", tc="TC-004")
@pytest.mark.uc("UC-027", flow="A2", tc="TC-004")
@pytest.mark.uc("UC-027", cond=["Pre-1", "Post-S-1", "Post-S-2"], tc="TC-004")
async def test_tc004_operator_stops_ffa_game(service_stack, headless_cleanup):
    stack = service_stack
    serials = await start_lobby(stack, count=4)
    game_client, game_channel = await get_game_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    collector = GameEventCollector(game_client)
    try:
        await collector.start()

        # Steps 1-2: the ready-up starts the game; game_started carries the id.
        game_id = headless_cleanup.add_game(await ready_up_until_started(stack, serials, collector, set()))
        await wait_running(game_client, game_id)

        # Step 3: an unknown id fails and the game keeps running.
        unknown = await game_client.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason=REASON, game_id="game_unknown")
        )
        assert not unknown.success
        assert "Unknown game_id" in unknown.error, unknown.error
        assert game_id in {g.game_id for g in await list_games(game_client)}, "the unknown-id stop ended the game"

        # Steps 4-5: Stop by id ends it with the reason and without a winner.
        stopped = await game_client.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason=REASON, game_id=game_id)
        )
        assert stopped.success, stopped.error
        await collector.wait_for_event("game_force_ended", timeout=EVENT_TIMEOUT_SECONDS)
        forced = _events(collector, game_id, "game_force_ended")
        assert [dict(e.data).get("reason") for e in forced] == [REASON], [dict(e.data) for e in forced]
        for absent in ("game_winner", "game_tie"):
            assert not _events(collector, game_id, absent), f"{absent} after a force end"
        assert game_id not in {g.game_id for g in await list_games(game_client)}, "session not retired"

        # Step 6: Stop again, no game in progress.
        again = await game_client.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason=REASON))
        assert not again.success
        assert "No game in progress" in again.error, again.error

        # Step 7: the menu restores the lobby; a new ready-up gets a new game id.
        await wait_for_lobby_colors(mock_client, serials, expected_color=FFA_LOBBY_COLOR, tolerance=2, timeout=10.0)
        known = set(started_ids(collector))
        restart_id = headless_cleanup.add_game(await ready_up_until_started(stack, serials, collector, known))
        assert restart_id != game_id
        await wait_running(game_client, restart_id)
        assert await force_end_game_by_id(game_client, restart_id, reason="tc004_cleanup")
        assert await async_poll_until(
            lambda: bool(_events(collector, restart_id, "game_force_ended")), timeout=EVENT_TIMEOUT_SECONDS
        )
    finally:
        await collector.stop()
        await mock_channel.close()
        await game_channel.close()
