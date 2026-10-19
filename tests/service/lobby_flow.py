"""Menu-driven game start for service-tier journeys (LOBBY composition).

The one home for the lobby flow of the service-tier journeys (TC-003, TC-004,
TC-005, TC-020; #1605). Ordering rule: restart the menu and select the mode
BEFORE the mock controllers are added (``start_lobby``).
"""

from proto import menu_pb2
from tests.integration.helpers import (
    ADMIN_WHITE,
    GameEventCollector,
    async_poll_until,
    get_menu_client,
    get_mock_client,
    mark_controllers_ready,
    select_game_mode,
    setup_mock_controllers,
    wait_for_lobby_colors,
    wait_until_running,
)

READY_TIMEOUT_SECONDS = 25.0
LOBBY_TIMEOUT_SECONDS = 15.0


def started_ids(collector: GameEventCollector) -> list[str]:
    return [e.game_id for e in collector.get_events("game_started") if e.game_id]


async def restart_menu_and_select(menu_client, game_mode: str) -> None:
    """Restart the menu and select ``game_mode`` on an open menu client."""
    await menu_client.StopMenu(menu_pb2.StopMenuRequest())
    started = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
    assert started.success, f"StartMenu failed: {started.error}"
    assert await select_game_mode(menu_client, game_mode), f"could not select {game_mode}"


async def start_lobby(stack, count: int = 4, game_mode: str = "JoustFFA") -> list[str]:
    """Precondition: the menu runs ``game_mode`` with ``count`` CONNECTED (not ready) controllers.

    The menu is restarted and the mode selected BEFORE the controllers are
    added. StopMenu clears the menu's controller states, and the controller
    manager publishes connect events on its next discovery tick (up to 1 s
    later): controllers added first could connect before the restart and be
    lost. Returns the serials once each shows its dim lobby colour (white does
    not count: the mock adapter starts every LED white, the same colour as admin).
    """
    menu_client, menu_channel = await get_menu_client(stack)
    mock_client, mock_channel = await get_mock_client(stack)
    try:
        await restart_menu_and_select(menu_client, game_mode)
        serials = await setup_mock_controllers(stack, count=count)
        await wait_for_lobby_colors(mock_client, serials, timeout=LOBBY_TIMEOUT_SECONDS, exclude=ADMIN_WHITE)
        return serials
    finally:
        for channel in (menu_channel, mock_channel):
            await channel.close()


async def ready_up_until_started(stack, serials, collector, known_ids) -> str:
    """Press the trigger on every controller; return the id of the NEW game that starts (one retry)."""
    mock_client, channel = await get_mock_client(stack)

    def new_id():
        return next((gid for gid in started_ids(collector) if gid not in known_ids), None)

    try:
        for _attempt in range(2):
            await mark_controllers_ready(mock_client, serials)
            if await async_poll_until(lambda: new_id() is not None, timeout=READY_TIMEOUT_SECONDS):
                return new_id()
    finally:
        await channel.close()
    seen = [(e.event_type, e.game_id) for e in collector.get_events()]
    raise AssertionError(f"no new game_started after ready-up (known {sorted(known_ids)}); events: {seen}")


async def wait_running(game_client, game_id: str, within_seconds: float = 20.0) -> None:
    running = await wait_until_running(game_client, {game_id}, timeout=within_seconds)
    assert game_id in running, f"{game_id} never RUNNING (running: {running})"
