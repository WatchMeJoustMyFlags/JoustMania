"""Menu-driven game start for service-tier journeys (LOBBY composition).

Used by the service-tier journeys TC-003 and TC-004 (#1597): the integration
suite's TC-002 carries the same steps inline. Dedupe with the shared lobby helpers once #1575 has merged.
"""

from proto import menu_pb2
from tests.integration.helpers import (
    GameEventCollector,
    async_poll_until,
    get_menu_client,
    get_mock_client,
    mark_controllers_ready,
    select_game_mode,
    wait_until_running,
)

READY_TIMEOUT_SECONDS = 25.0


def started_ids(collector: GameEventCollector) -> list[str]:
    return [e.game_id for e in collector.get_events("game_started") if e.game_id]


async def open_lobby(stack, game_mode: str = "JoustFFA") -> None:
    """Restart the menu so it registers the controllers now connected, then select ``game_mode``."""
    menu_client, channel = await get_menu_client(stack)
    try:
        await menu_client.StopMenu(menu_pb2.StopMenuRequest())
        started = await menu_client.StartMenu(menu_pb2.StartMenuRequest())
        assert started.success, f"StartMenu failed: {started.error}"
        assert await select_game_mode(menu_client, game_mode), "select_game rejected"
    finally:
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
