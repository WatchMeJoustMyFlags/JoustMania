"""
UC-030 A7: a player holds the shield in a hold-mode FFA game, end to end (#2096).

The real stack on unreserved mock controllers: an operator stage overlay selects
``shield.activation=hold`` (a stack-only flag, UC-040 BR-019) with a full starting
pool, the menu starts an FFA game, and ``SimulateButton`` holds and releases the
trigger. Observable only: the controller's LED and rumble (UC-040 BR-026) and the
game state. Latency is not asserted here (UC-030 BR-004: only the order of events).
"""

import asyncio
import os
import sys

import pytest
from openfeature.evaluation_context import EvaluationContext

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from proto import controller_manager_mock_pb2
from tests.integration.helpers import (
    GameEventCollector,
    ObservabilityObserver,
    async_poll_until,
    flagd_probe_client,
    get_controller_serials,
    get_game_client,
    get_mock_client,
    get_player_states,
    kill_player_verified,
    poll_until,
    setup_mock_controllers,
    start_game_via_menu,
    wait_until_running,
)

_WHITE = (255, 255, 255)
_HOLD_RUMBLE = 70  # shield.hold_rumble_intensity default (flags.md §4)
_TRIGGER = controller_manager_mock_pb2.ButtonRequest.Button.TRIGGER


async def _trigger(mock_client, serial: str, pressed: bool) -> None:
    await mock_client.SimulateButton(
        controller_manager_mock_pb2.ButtonRequest(
            serial=serial, button=_TRIGGER, pressed=pressed
        )
    )


def _since(observer: ObservabilityObserver, serial: str, mark: int):
    """LED colours and rumble intensities on ``serial`` from observer event ``mark`` on."""
    events = [e for e in observer.get_events()[mark:] if e.serial == serial]
    leds = [
        (e.led_change.r, e.led_change.g, e.led_change.b)
        for e in events
        if e.HasField("led_change")
    ]
    rumbles = [e.rumble_change.intensity for e in events if e.HasField("rumble_change")]
    return leds, rumbles


async def _alive(game_client, game_id: str, serial: str) -> bool:
    players = await get_player_states(game_client, game_id=game_id)
    return bool(players) and players[serial].alive


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="A7", br=["BR-008"])
@pytest.mark.uc("UC-040", br=["BR-019", "BR-026"])
async def test_uc030_a7_holding_the_trigger_protects_and_shows_white_with_a_rumble(
    docker_compose, ensure_game_stopped, flag_service
):
    """UC-030 A7: held = white LED, light rumble, no death; released = rumble off, the next death lands."""
    mock_client, _mock_channel = await get_mock_client(docker_compose)
    game_client, _game_channel = await get_game_client(docker_compose)

    # Step 1: the operator selects hold with a full starting pool (stage overlays, cleared at teardown).
    flag_service.put("game", "shield.activation", variant="hold")
    flag_service.put("game", "shield.energy_initial_seconds", variant="full")
    with flagd_probe_client(docker_compose, "game") as probe:
        assert poll_until(
            lambda: (
                probe.get_string_value("shield.activation", "", EvaluationContext())
                == "hold"
                and probe.get_float_value(
                    "shield.energy_initial_seconds", 0.0, EvaluationContext()
                )
                == 5.0
            ),
        ), "Step 1: flagd did not serve shield.activation=hold with a 5 s starting pool"

    await setup_mock_controllers(docker_compose, count=2)
    collector = GameEventCollector(game_client)
    observer = ObservabilityObserver(mock_client)
    holder = None
    try:
        await collector.start()
        await observer.start()

        # Step 2: an FFA game through the menu (a real game: shadow games never run hold).
        await start_game_via_menu(docker_compose, "JoustFFA", event_collector=collector)
        game_id = collector.get_events("game_started")[-1].game_id
        serials = await get_controller_serials(docker_compose)
        running = await wait_until_running(game_client, {game_id}, timeout=15.0)
        assert game_id in running, f"Step 2: {game_id} never RUNNING ({running})"
        holder = serials[0]
        await asyncio.sleep(
            1.0
        )  # past the 0.3 s start grace; the trigger reads released once (armed)

        # Step 3: hold the trigger: the controller turns white and rumbles lightly (UC-040 BR-026).
        mark = len(observer.get_events())
        await _trigger(mock_client, holder, True)

        def _held_feedback() -> bool:
            leds, rumbles = _since(observer, holder, mark)
            return _WHITE in leds and _HOLD_RUMBLE in rumbles

        assert await async_poll_until(_held_feedback, timeout=5.0), (
            f"Step 3: no white LED with the {_HOLD_RUMBLE} hold rumble on {holder}: "
            f"{_since(observer, holder, mark)}"
        )

        # Step 4: a death-level motion while held does not kill (UC-030 A7 step 2).
        for _ in range(3):
            response = await mock_client.SimulateDeath(
                controller_manager_mock_pb2.DeathRequest(serial=holder)
            )
            assert response.success
            await asyncio.sleep(0.2)
        assert await _alive(game_client, game_id, holder), (
            f"Step 4: {holder} died while holding the shield"
        )

        # Step 5: release: the hold rumble stops and the LED leaves white (UC-030 A7 steps 3-4).
        mark = len(observer.get_events())
        await _trigger(mock_client, holder, False)

        def _released_feedback() -> bool:
            leds, rumbles = _since(observer, holder, mark)
            return 0 in rumbles and bool(leds) and leds[-1] != _WHITE

        assert await async_poll_until(_released_feedback, timeout=5.0), (
            f"Step 5: the hold feedback did not end on {holder}: {_since(observer, holder, mark)}"
        )

        # Step 6: the protection is gone: the next death lands.
        landed = await kill_player_verified(
            mock_client, game_client, holder, lambda p: not p.alive, game_id=game_id
        )
        assert landed or game_id not in await wait_until_running(
            game_client, {game_id}, timeout=0
        ), f"Step 6: {holder} could not be killed after the release"
    finally:
        if holder is not None:
            await _trigger(mock_client, holder, False)
        await observer.stop()
        await collector.stop()
