"""UC-002 A5 on the service tier (T2): the menu's button stream drops and reconnects.

Real controller manager, game coordinator and menu from source (LOBBY). The
menu reaches the controller manager through a ``TcpProxy`` (its
``CONTROLLER_MANAGER_PORT``), so the test cuts the connection without
restarting either process. Three controllers sit in the lobby, two of them
READY. After the drop the menu clears its lobby (step 1), the controller
manager re-sends a connect for every controller on the new stream (step 2) and
the menu registers each one again as CONNECTED (step 3): the READY controllers
are back to the dim lobby colour, and a ready-up afterwards starts a game with
all three, which only happens when the third controller is in the lobby too
(readying the first two would otherwise start a two-player game).
"""

import asyncio
import dataclasses

import pytest

from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    get_controller_color,
    get_game_client,
    get_mock_client,
    get_player_states,
    mark_controllers_ready,
)
from tests.service.harness import LOBBY, SERVICES
from tests.service.harness.fakes import TcpProxy
from tests.service.lobby_flow import ready_up_until_started, start_lobby, started_ids, wait_running

pytestmark = pytest.mark.tier("service")

# Bound at import: the menu's address goes into the composition before the stack starts.
BUTTON_STREAM_PROXY = TcpProxy()
SERVICE_COMPOSITION = dataclasses.replace(
    LOBBY, env={"menu": {"CONTROLLER_MANAGER_PORT": str(BUTTON_STREAM_PROXY.port)}}
)

LED_TIMEOUT_SECONDS = 15.0
RECONNECT_TIMEOUT_SECONDS = 15.0


@pytest.fixture(scope="module")
def button_stream_proxy(service_stack):
    """The proxy forwards to the stack's controller manager; the menu's first dials wait in its backlog."""
    BUTTON_STREAM_PROXY.start(service_stack.get_service_port("controller-manager", SERVICES["controller-manager"][1]))
    yield BUTTON_STREAM_PROXY
    BUTTON_STREAM_PROXY.stop()


async def _colors(mock_client, serials) -> dict[str, tuple[int, int, int]]:
    return {serial: await get_controller_color(mock_client, serial) for serial in serials}


@pytest.mark.uc("UC-002", flow="A5", steps=[1, 2, 3])
async def test_button_stream_reconnect_re_registers_every_controller(
    service_stack, button_stream_proxy, headless_cleanup
):
    stack = service_stack
    serials = await start_lobby(stack, count=3)
    readied = serials[:2]
    mock_client, mock_channel = await get_mock_client(stack)
    game_client, game_channel = await get_game_client(stack)
    collector = GameEventCollector(game_client)
    try:
        await collector.start()
        dim = await _colors(mock_client, serials)

        # Two of three READY: their LEDs leave the dim lobby colour; no game starts.
        await mark_controllers_ready(mock_client, readied)

        async def readied_lit() -> bool:
            colors = await _colors(mock_client, readied)
            return all(colors[serial] != dim[serial] for serial in readied)

        assert await async_poll_until(readied_lit, timeout=LED_TIMEOUT_SECONDS), "READY never showed on the LEDs"

        # The button stream drops; the menu dials the controller manager again.
        dialled = button_stream_proxy.accepted
        assert button_stream_proxy.drop_connections() > 0, "the menu had no connection through the proxy"
        assert await asyncio.to_thread(
            button_stream_proxy.wait_for_connections, dialled + 1, RECONNECT_TIMEOUT_SECONDS
        ), "the menu never reconnected"

        # Steps 1-3: every controller is CONNECTED again with the dim lobby colour (READY cleared).
        last = {}

        async def all_dim_recorded() -> bool:
            last.update(await _colors(mock_client, serials))
            return last == dim

        assert await async_poll_until(all_dim_recorded, timeout=LED_TIMEOUT_SECONDS), (
            f"lobby not re-registered after the reconnect: {last} != {dim}"
        )

        # Every controller is in the lobby: the game starts only once all three are READY.
        game_id = headless_cleanup.add_game(
            await ready_up_until_started(stack, serials, collector, set(started_ids(collector)))
        )
        await wait_running(game_client, game_id)
        players = await get_player_states(game_client, game_id=game_id)
        assert players is not None
        assert set(players) == set(serials)
    finally:
        await collector.stop()
        await game_channel.close()
        await mock_channel.close()
