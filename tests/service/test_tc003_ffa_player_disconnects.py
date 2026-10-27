"""TC-003 on the service tier (T2): a controller disconnects during an FFA game.

Real controller manager, game coordinator and menu from source (LOBBY, OTel
exported to the in-process OtlpSink), three unreserved mock controllers readied
through the menu. The mock ``RemoveController`` drops one mid-game; the
coordinator eliminates it through the death path (UC-020 A2) and the player
span carries ``player_disconnected`` (UC-030 A5). The game goes on, a second
death leaves the last player as winner, and the lobby holds the two survivors.
"""

import dataclasses

import pytest

from proto import controller_manager_mock_pb2, controller_manager_pb2
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    get_controller_client,
    get_game_client,
    get_mock_client,
    get_player_states,
    kill_player_verified,
)
from tests.service.harness import LOBBY
from tests.service.lobby_flow import ready_up_until_started, start_lobby, wait_running

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = dataclasses.replace(LOBBY, otlp=True)

COORDINATOR = "game-coordinator-service"
CONTROLLER_MANAGER = "controller-manager-service"
EVENT_TIMEOUT_SECONDS = 15.0
# The BatchSpanProcessor and the metric reader flush on an interval (5 s by default).
EXPORT_TIMEOUT_SECONDS = 30.0


def _events(collector: GameEventCollector, game_id: str, event_type: str) -> list:
    return [e for e in collector.get_events(event_type) if e.game_id == game_id]


@pytest.mark.uc("UC-003", flow="main", steps=[1, 3, 4, 5], tc="TC-003")
@pytest.mark.uc("UC-003", flow="A4", steps=[1], tc="TC-003")
@pytest.mark.uc("UC-003", tc="TC-003", cond=["Post-S-1"])
@pytest.mark.uc("UC-010", flow="main", tc="TC-003")
@pytest.mark.uc("UC-020", flow="A2", tc="TC-003")
@pytest.mark.uc("UC-030", flow="A5", tc="TC-003")
@pytest.mark.uc("UC-030", tc="TC-003", cond=["Post-S-1"])
@pytest.mark.uc("UC-002", flow="A1", tc="TC-003", cond=["Post-F-1"])
async def test_tc003_ffa_player_disconnects(service_stack, headless_cleanup):
    stack = service_stack
    sink = stack.otlp_sink
    assert sink is not None

    # Step 1: three unreserved controllers (menu restarted and FFA selected first).
    serials = await start_lobby(stack, count=3)
    leaver, killed, winner = serials[1], serials[0], serials[2]
    game_client, game_channel = await get_game_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    controller_client, controller_channel = await get_controller_client(stack)
    collector = GameEventCollector(game_client)
    try:
        await collector.start()

        # Step 2: ready-up starts FFA. The countdown's EMA warmup drops frames
        # (disconnects included): remove the controller once the loop runs.
        game_id = headless_cleanup.add_game(await ready_up_until_started(stack, serials, collector, set()))
        await wait_running(game_client, game_id)
        await sink.wait_metric_async(
            "game_actual_update_frequency_hz", {"game_id": game_id}, service=COORDINATOR, timeout=EXPORT_TIMEOUT_SECONDS
        )

        # Step 3: the controller disconnects.
        removed = await mock_client.RemoveController(controller_manager_mock_pb2.RemoveControllerRequest(serial=leaver))
        assert removed.success, removed.error

        # Step 4: a death with two players left, and player_disconnected on the player span.
        await collector.wait_for_event("player_death", timeout=EVENT_TIMEOUT_SECONDS)
        deaths = _events(collector, game_id, "player_death")
        assert [(d.data["serial"], d.data["alive_remaining"]) for d in deaths] == [(leaver, "2")]
        span = await sink.wait_span_async(
            None,
            {"game.id": game_id, "player.serial": leaver},
            service=COORDINATOR,
            event="player_disconnected",
            timeout=EXPORT_TIMEOUT_SECONDS,
        )
        disconnected = next(e for e in span.events if e.name == "player_disconnected")
        assert disconnected.attributes == {"reason": "controller_disconnect"}

        # Step 5: the game keeps running with the other two.
        assert not _events(collector, game_id, "game_ended")
        await wait_running(game_client, game_id)
        players = await get_player_states(game_client, game_id=game_id)
        assert players is not None
        assert {serial for serial, player in players.items() if player.alive} == {killed, winner}

        # Steps 6-7: another death leaves the last player the winner.
        await kill_player_verified(mock_client, game_client, killed, lambda p: not p.alive, game_id=game_id)
        await collector.wait_for_event("game_ended", timeout=EVENT_TIMEOUT_SECONDS)
        winners = _events(collector, game_id, "game_winner")
        assert [w.data["serial"] for w in winners] == [winner]
        assert [d.data["serial"] for d in _events(collector, game_id, "player_death")] == [leaver, killed]

        # Step 8: the controller manager's roster holds the survivors only (no ghost).
        async def survivors_only() -> bool:
            roster = await controller_client.GetConnectedControllers(
                controller_manager_pb2.GetConnectedControllersRequest()
            )
            return {killed, winner} <= set(roster.connected_serials) and leaver not in roster.connected_serials

        assert await async_poll_until(survivors_only, timeout=EVENT_TIMEOUT_SECONDS), "the roster still lists a ghost"
        gauge = await sink.wait_metric_async(
            "controller_connected",
            {"serial": leaver},
            service=CONTROLLER_MANAGER,
            predicate=lambda p: p.value == 0,
            timeout=EXPORT_TIMEOUT_SECONDS,
        )
        assert gauge.value == 0
    finally:
        await collector.stop()
        await controller_channel.close()
        await mock_channel.close()
        await game_channel.close()
