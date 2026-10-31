"""Pin the game-time waits that go through ``game_clock.sleep`` (#2361).

The modes wait on ``game_clock.sleep`` so the test clock can run them faster. A
recording replacement here proves each wait keeps its production duration and order,
so a shortened wait cannot hide behind the autouse 10x clock. Every test opts out of
the shortened constants with ``real_durations`` and records instead of waiting.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import EventCollector, MockControllerManagerService

from services.game_coordinator import game_clock
from services.game_coordinator.games import fight_club as fight_club_mod
from services.game_coordinator.games import nonstop_joust as nonstop_mod
from services.game_coordinator.games import tournament as tournament_mod
from services.game_coordinator.games import traitor as traitor_mod
from services.game_coordinator.games import zombie as zombie_mod
from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.fight_club import FightClubGame
from services.game_coordinator.games.nonstop_joust import NonstopJoustGame
from services.game_coordinator.games.swapper import SwapperGame
from services.game_coordinator.games.tournament import TournamentGame
from services.game_coordinator.games.traitor import TraitorGame
from services.game_coordinator.games.werewolf import WerewolfGame
from services.game_coordinator.games.zombie import ZombieGame

pytestmark = [pytest.mark.real_durations, pytest.mark.asyncio]


class _Stream:
    async def write(self, _message):
        pass


class SleepRecorder:
    """Stands in for ``game_clock.sleep``: records each wait, returns at once."""

    def __init__(self, monkeypatch, on_sleep=None):
        self.waits: list[float] = []
        self._on_sleep = on_sleep
        monkeypatch.setattr(game_clock, "sleep", self._sleep)

    async def _sleep(self, seconds):
        self.waits.append(seconds)
        if self._on_sleep:
            self._on_sleep()


async def _game(cls, players=4, **kwargs):
    manager = MockControllerManagerService(num_controllers=players, death_schedule={}, max_duration=10.0)
    game = cls(
        controller_manager_client=manager,
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_game_clock_waits",
        **kwargs,
    )
    game._play_sound = AsyncMock()
    game.gameplay_stream = _Stream()
    await game._initialize_players_impl(manager.controllers)
    game.running = True
    return game


@pytest.mark.uc("UC-025", flow="A2", steps=[1])
async def test_fight_club_countdown_ticks_every_second(monkeypatch):
    game = await _game(FightClubGame, players=2)
    game.round_end_time = game_clock.now() + 100
    recorder = SleepRecorder(monkeypatch, on_sleep=lambda: setattr(game, "running", False))

    await game._round_timer()

    assert recorder.waits == [1.0]


@pytest.mark.uc("UC-025", flow="A2", steps=[1])
async def test_fight_club_round_pause_is_one_second(monkeypatch):
    game = await _game(FightClubGame, players=2)
    recorder = SleepRecorder(monkeypatch)

    await game._pause_between_rounds()

    assert fight_club_mod.ROUND_PAUSE_SECONDS == 1.0
    assert recorder.waits == [1.0]


@pytest.mark.uc("UC-023", flow="main", steps=[2])
async def test_zombie_timer_ticks_every_second(monkeypatch):
    game = await _game(ZombieGame)
    game.time_remaining = 100
    recorder = SleepRecorder(monkeypatch, on_sleep=lambda: setattr(game, "running", False))

    await game._game_timer()

    assert recorder.waits == [1.0]


@pytest.mark.uc("UC-023", flow="main", steps=[3])
async def test_zombie_conversion_colour_waits_half_a_second(monkeypatch):
    game = await _game(ZombieGame)
    recorder = SleepRecorder(monkeypatch)

    await game._show_zombie_colour_after_flash("not_a_player")

    assert zombie_mod.CONVERSION_COLOUR_DELAY == 0.5
    assert recorder.waits == [0.5]


@pytest.mark.uc("UC-023", flow="main", steps=[5])
async def test_zombie_respawn_waits_the_drawn_delay(monkeypatch):
    game = await _game(ZombieGame)
    recorder = SleepRecorder(monkeypatch)

    with patch.object(game, "_respawn_player", new_callable=AsyncMock):
        await game._respawn_zombie("mock_controller_0", 4.5)

    assert recorder.waits == [4.5]


@pytest.mark.uc("UC-022", flow="A1", steps=[2])
async def test_werewolf_reveal_waits_the_reveal_time_then_flashes_each_wolf(monkeypatch):
    game = await _game(WerewolfGame, players=6, reveal_time_seconds=7.0)
    recorder = SleepRecorder(monkeypatch)

    await game._reveal_werewolves()

    wolves = len(game.werewolf_serials)
    assert wolves >= 2
    assert recorder.waits == [7.0] + [0.3] * wolves


@pytest.mark.uc("UC-024", flow="main", steps=[2])
async def test_swapper_swap_flash_waits_half_a_second(monkeypatch):
    game = await _game(SwapperGame)
    recorder = SleepRecorder(monkeypatch)

    await game._swap_pause()

    assert recorder.waits == [0.5]


@pytest.mark.uc("UC-024", flow="main", steps=[3])
async def test_swapper_celebration_is_twenty_tenths(monkeypatch):
    game = await _game(SwapperGame)
    for player in game.players.values():
        player.team = 0
    game.last_death_serial = "mock_controller_3"
    game.state = GameState.RUNNING
    assert await game._check_win_condition()
    recorder = SleepRecorder(monkeypatch)

    await game._end_game_impl()

    assert recorder.waits == [0.1] * 20


@pytest.mark.uc("UC-025", flow="main", steps=[1])
async def test_tournament_intro_waits_in_tenths(monkeypatch):
    game = await _game(TournamentGame)
    recorder = SleepRecorder(monkeypatch)

    await game._tournament_intro_phase()

    assert tournament_mod.TOURNAMENT_INTRO_DURATION_S == 3.0
    assert recorder.waits == [0.1] * 30


@pytest.mark.uc("UC-022", flow="main", steps=[2])
async def test_traitor_signal_waits_in_tenths(monkeypatch):
    game = await _game(TraitorGame, players=6)
    recorder = SleepRecorder(monkeypatch)

    await game._traitor_signal_phase()

    assert traitor_mod.TRAITOR_SIGNAL_DURATION == 5
    assert recorder.waits == [0.1] * 50


@pytest.mark.uc("UC-026", flow="main", steps=[3])
async def test_nonstop_winner_hold_ticks_in_tenths(monkeypatch):
    game = await _game(NonstopJoustGame)
    game.state = GameState.RUNNING
    game._time_limit_reached = True  # only a time-limit end has a winner (UC-026 BR-003)
    recorder = SleepRecorder(monkeypatch)

    await game._end_game_impl()

    assert nonstop_mod.WINNER_HOLD_SECONDS == 2.0
    assert recorder.waits == [0.1] * 20
