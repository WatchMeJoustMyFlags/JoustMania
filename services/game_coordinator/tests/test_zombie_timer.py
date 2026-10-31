"""UC-023 step 2 / BR-001 / BR-005: Zombies timer, announcements and win-check precedence."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import EventCollector, MockControllerManagerService

from lib.types import Sound
from services.game_coordinator.games.zombie import ZombieGame, calculate_game_duration

MARK_SOUNDS = {
    60: Sound.VOX_ZOMBIE_ONE_MINUTE,
    30: Sound.VOX_ZOMBIE_THIRTY_SECONDS,
    10: Sound.VOX_ZOMBIE_TEN_SECONDS,
}


def _make_game(num_players: int = 4):
    manager = MockControllerManagerService(num_controllers=num_players, death_schedule={}, max_duration=10.0)
    events = EventCollector()
    game = ZombieGame(
        controller_manager_client=manager,
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_zombie_timer_marks",
    )
    game._play_sound = AsyncMock()
    return game, manager, events


async def _run_timer(game) -> None:
    """Run the real _game_timer without waiting: each tick only yields to the loop."""
    real_sleep = asyncio.sleep

    async def instant_tick(_seconds):
        await real_sleep(0)

    game.running = True
    with patch("services.game_coordinator.game_clock.sleep", instant_tick):
        await game._game_timer()


@pytest.mark.uc("UC-023", flow="main", steps=[2], br=["BR-001"])
@pytest.mark.parametrize("num_players", range(2, 17))
@pytest.mark.asyncio
async def test_each_timer_mark_is_announced_exactly_once(num_players):
    game, _, events = _make_game()
    game.time_remaining = calculate_game_duration(num_players)
    duration = game.time_remaining

    await _run_timer(game)

    announced = [e["seconds_remaining"] for e in events.get_events_of_type("time_announcement")]
    assert sorted(announced) == sorted(m for m in MARK_SOUNDS if m < duration)
    played = [call.args[0] for call in game._play_sound.await_args_list]
    assert sorted(played, key=lambda s: s.value) == sorted((MARK_SOUNDS[m] for m in announced), key=lambda s: s.value)
    assert all(call.kwargs == {"priority": 2} for call in game._play_sound.await_args_list)


@pytest.mark.uc("UC-023", flow="main", steps=[2], br=["BR-001"])
@pytest.mark.parametrize(
    ("num_players", "seconds"),
    [(2, 22.5), (4, 45.0), (8, 90.0), (12, 135.0), (16, 180.0)],
)
def test_duration_formula_real_values(num_players, seconds):
    assert calculate_game_duration(num_players) == seconds


@pytest.mark.uc("UC-023", flow="main", steps=[2], br=["BR-001"])
@pytest.mark.asyncio
async def test_no_180_second_announcement():
    game, _, events = _make_game()
    game.time_remaining = 181.0

    await _run_timer(game)

    announced = [e["seconds_remaining"] for e in events.get_events_of_type("time_announcement")]
    assert 180 not in announced
    assert sorted(announced) == [10, 30, 60]


@pytest.mark.uc("UC-023", flow="main", steps=[2], br=["BR-005"])
@pytest.mark.asyncio
async def test_no_announcement_after_the_win_is_latched():
    game, _, events = _make_game()
    game._winner_side = "zombies"
    game.time_remaining = 61.0

    await _run_timer(game)

    assert events.get_events_of_type("time_announcement") == []
    game._play_sound.assert_not_awaited()


@pytest.mark.uc("UC-023", flow="main", steps=[5], br=["BR-005"])
@pytest.mark.asyncio
async def test_time_up_with_no_humans_left_gives_the_zombies_the_win():
    game, manager, events = _make_game()
    await game._initialize_players_impl(manager.controllers)
    for serial in list(game.human_serials):
        game.players[serial].is_zombie = True
    game.time_remaining = 0.0

    assert await game._check_win_condition() is True

    winners = events.get_events_of_type("zombie_winner")
    assert [w["winner"] for w in winners] == ["zombies"]
