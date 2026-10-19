"""TC-012 on the service tier (T2): pattern-driven shadow games replay identically.

Moved from tests/integration/test_pattern_games.py (#1563; UC-046, #1258).
Runs the same archetype twice on real services from source (controller manager
+ game coordinator, GAME composition; audio faked) with reserved mock controllers driven by the parametric motion
function, and checks what the replay check relies on:

- the controller manager's roster (GetConnectedControllers, the menu's view of
  the lobby) never lists the reserved pattern controllers while a lobby
  controller on the same manager is listed (TC-012 step 2);
- the controller manager installs the archetype and refuses scripted motion for
  pattern controllers (BR-004);
- the game ends organically with a winner (no kill commands are sent);
- per-slot checkpoint hashes are identical on their common prefix (NFR-040);
- the elimination order derived from delivered-frame counts (BR-014) and the
  winner slot are identical in both runs (NFR-041).

The agent is not involved: the test reserves the controllers and starts the game
itself, exactly as the shadow runner does for a one-shot pattern game.
"""

import asyncio
import json

import grpc
import pytest

from proto import controller_manager_mock_pb2, controller_manager_pb2
from tests.integration.helpers import (
    async_poll_until,
    build_start_config,
    get_controller_client,
    get_game_client,
    get_mock_client,
    quiesce_coordinator,
    start_game_headless,
)
from tests.service.harness import GAME

pytestmark = pytest.mark.tier("service")

SERVICE_COMPOSITION = GAME

# A fast cascade: slot 2 drifts fastest and crosses the default 1.8 g threshold
# first (about 10 s of game time), then slot 1; slot 0 drifts slowest and wins.
ARCHETYPE = "it_fast_cascade"
PARAMS = {
    "players": 3,
    "stream_rate_hz": 30,
    "base": 1.0,
    "drift": 0.02,
    "amp": 0.05,
    "period_s": 3.0,
    "phase": 0.0,
    "burst_h": 0.0,
    "burst_period_s": 0.0,
    "d_base": 0.0,
    "d_drift": 0.03,
    "d_amp": 0.0,
    "d_phase": 0.25,
}
SEED = 1258
GAME_TIMEOUT_S = 120


def _elimination_order(run: dict) -> tuple[list[int], int]:
    """UC-046 BR-014: the order of the coordinator's player_death events; the winner from game_winner."""
    return run["death_slots"], run["winner_slot"]


async def _roster(controller_client) -> set[str]:
    response = await controller_client.GetConnectedControllers(controller_manager_pb2.GetConnectedControllersRequest())
    return set(response.connected_serials)


async def _run_pattern_game(stack, tag: str) -> dict:
    mock, mock_channel = await get_mock_client(stack)
    game, game_channel = await get_game_client(stack)
    controller, controller_channel = await get_controller_client(stack)
    try:
        return await _play(mock, game, controller, tag)
    finally:
        await mock_channel.close()
        await game_channel.close()
        await controller_channel.close()


async def _play(mock, game, controller, tag: str) -> dict:
    await quiesce_coordinator(game, context=f"pattern game {tag}")

    added = await mock.AddControllers(
        controller_manager_mock_pb2.AddControllersRequest(
            count=PARAMS["players"],
            reserved=True,
            tag=f"it:pattern:{tag}",
            archetype=ARCHETYPE,
            archetype_params=json.dumps(PARAMS),
        )
    )
    assert added.success, added.error
    assert (added.archetype, added.stream_rate_hz, added.players) == (ARCHETYPE, 30, 3)
    serials = list(added.serials)

    # TC-012 step 2: reserved controllers stay out of the lobby. A plain lobby
    # controller on the same manager proves the roster is not just empty.
    lobby = await mock.AddControllers(controller_manager_mock_pb2.AddControllersRequest(count=1))
    assert lobby.success, lobby.error
    lobby_serials = set(lobby.serials)

    async def roster_is_lobby() -> bool:
        return await _roster(controller) == lobby_serials

    assert await async_poll_until(roster_is_lobby, timeout=10.0), (
        f"the roster must hold only the lobby controller {lobby_serials}, got {await _roster(controller)}"
    )

    # BR-004: scripted motion is refused for pattern controllers.
    with pytest.raises(grpc.aio.AioRpcError) as refused:
        await mock.SimulateDeath(controller_manager_mock_pb2.DeathRequest(serial=serials[0]))
    assert refused.value.code() == grpc.StatusCode.FAILED_PRECONDITION

    config = build_start_config("JoustFFA", serials)
    config.rng_seed = SEED
    game_id, collector = await start_game_headless(game, config)
    try:
        assert await _roster(controller) == lobby_serials, "a game start leaked reserved controllers into the roster"
        terminal = await collector.wait_for_any_event(
            ["game_ended", "game_force_ended", "game_error"], timeout=GAME_TIMEOUT_S
        )
        winners = [dict(e.data).get("serial") for e in collector.get_events("game_winner")]
        deaths = [dict(e.data).get("serial") for e in collector.get_events("player_death")]
    finally:
        await collector.stop()

    records = []
    for serial in serials:
        resp = await mock.RemoveController(controller_manager_mock_pb2.RemoveControllerRequest(serial=serial))
        assert resp.success and resp.archetype == ARCHETYPE
        records.append(
            {
                "slot": resp.pattern_slot,
                "frames": resp.frames_delivered,
                "checkpoints": list(resp.signal_checkpoints),
                "overruns": resp.frame_overruns,
            }
        )
    for serial in lobby_serials:
        await mock.RemoveController(controller_manager_mock_pb2.RemoveControllerRequest(serial=serial))
    records.sort(key=lambda r: r["slot"])
    winner_slot = serials.index(winners[0]) if winners and winners[0] in serials else -1
    death_slots = [serials.index(sr) for sr in deaths if sr in serials]
    return {
        "game_id": game_id,
        "terminal": terminal.event_type,
        "records": records,
        "winner_slot": winner_slot,
        "death_slots": death_slots,
    }


@pytest.mark.asyncio
@pytest.mark.uc(
    "UC-046",
    flow="main",
    steps=[3, 4, 5, 6, 8],
    br=["BR-001", "BR-004", "BR-005", "BR-014"],
    tc="TC-012",
)
async def test_pattern_game_replays_identically(service_stack):
    """UC-046 main: the same archetype and seed twice give identical checkpoints and elimination order."""
    first = await _run_pattern_game(service_stack, "a")
    await asyncio.sleep(1.0)
    second = await _run_pattern_game(service_stack, "b")

    for run in (first, second):
        assert run["terminal"] == "game_ended", run
        assert [r["slot"] for r in run["records"]] == [0, 1, 2]
        assert all(r["frames"] > 30 * 5 for r in run["records"]), run["records"]

    # NFR-040: checkpoints equal on the common prefix, which covers the deaths.
    for a, b in zip(first["records"], second["records"], strict=True):
        common = min(len(a["checkpoints"]), len(b["checkpoints"]))
        assert common >= 5, (a, b)
        assert a["checkpoints"][:common] == b["checkpoints"][:common], f"slot {a['slot']} diverged"

    # NFR-041 / BR-014: same elimination order and winner; the fastest drift dies first.
    order_a, winner_a = _elimination_order(first)
    order_b, winner_b = _elimination_order(second)
    detail = {"first": first, "second": second}
    assert (order_a, winner_a) == (order_b, winner_b), detail
    assert (order_a, winner_a) == ([2, 1], 0), detail
