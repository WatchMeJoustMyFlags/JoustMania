"""Player analytics in every game mode (#2358, UC-041 step 4).

The agent's per-player movement and skill inputs (``game_player_movement_variance``,
``game_player_skill_level``, ``game_player_playstyle``, ``game_player_peak_accel``, ...)
come from a ``PlayerAnalytics`` tracker per player. Before #2358 only FFA, Teams,
Random Teams and NonStop created one, so the other six modes fed the agent nothing.
The tracker is now attached once in ``BaseGameMode._initialize_players`` and the
end-of-game ``player_analytics`` summary is published by every mode.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, MockGameplayStream
from test_end_effect_closed_stream import _make_winner

from lib.types import GameEvent, Games
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.game_factory import _GAME_CLASSES
from services.game_coordinator.games.base import GameState
from services.game_coordinator.games.fight_club import FightState
from services.game_coordinator.games.tournament import TournamentState

ALL_MODES = list(_GAME_CLASSES)
# The modes that produced analytics before #2358; their output must not change.
LEGACY_MODES = [Games.JoustFFA, Games.JoustTeams, Games.JoustRandomTeams, Games.NonStop]
FIXED_NOW = 100.0
# Per-player accel magnitudes (g), all below the warning threshold: a fixed, varied motion.
MOTION = [1.0, 1.08, 1.16, 1.02, 1.12, 1.04]


def _frame(serial: str, magnitude: float) -> controller_manager_pb2.GameplayData:
    return controller_manager_pb2.GameplayData(
        serial=serial,
        accel=controller_manager_pb2.Vector3(x=0, y=0, z=magnitude),
        gyro=controller_manager_pb2.Vector3(x=0, y=0, z=0),
    )


async def _started_game(mode: Games, events: EventCollector):
    """A RUNNING game of ``mode`` whose players went through the base initialisation."""
    cm = MockControllerManagerService(num_controllers=4)
    game = _GAME_CLASSES[mode](
        controller_manager_client=cm,
        event_publisher=events.publish,
        audio_client=None,
        game_id=f"test_analytics_{mode.name.lower()}",
        initial_players=[type("P", (), {"serial": c.serial})() for c in cm.controllers],
    )
    await game._initialize_players()
    game.state = GameState.RUNNING
    game.start_time = FIXED_NOW
    game.running = True
    game.gameplay_stream = MockGameplayStream()
    game._play_sound = AsyncMock()
    game._wait_for_rainbow_effect = AsyncMock(return_value=True)
    return game


def _activate(game, mode: Games) -> None:
    """Put every player in the state where its motion is checked (fighters only in the duel modes)."""
    for player in game.players.values():
        if mode == Games.FightClub:
            player.state = FightState.FIGHTER
        elif mode == Games.Tournament:
            player.tournament_state = TournamentState.FIGHTING


async def _feed_motion(game, mode: Games) -> None:
    _activate(game, mode)
    for frame_index in range(len(MOTION)):
        for player_index, serial in enumerate(sorted(game.players)):
            magnitude = MOTION[(frame_index + player_index) % len(MOTION)] + 0.03 * player_index
            await game._process_controller_state(_frame(serial, magnitude))


async def _end_won_game(game, mode: Games) -> None:
    if mode == Games.Tournament:
        # The champion check below wants the others out of the bracket.
        for player in game.players.values():
            player.tournament_state = TournamentState.WAITING
    _make_winner(game, mode)
    if mode not in (Games.FightClub, Games.Tournament, Games.NonStop):
        assert await game._check_win_condition()
    game._latch_game_outcome()
    game.running = False
    await game._end_game_impl()


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(game_clock, "now", lambda: FIXED_NOW)


@pytest.mark.uc("UC-041", flow="main", br=["BR-006"])
@pytest.mark.parametrize("mode", ALL_MODES, ids=[m.name for m in ALL_MODES])
@pytest.mark.asyncio
async def test_every_mode_attaches_player_analytics(mode):
    """Every player of every mode gets a tracker at initialisation (UC-041 step 4)."""
    game = await _started_game(mode, EventCollector())
    assert game.players
    assert all(player.analytics is not None for player in game.players.values())


@pytest.mark.uc("UC-041", flow="main", br=["BR-006"])
@pytest.mark.parametrize("mode", ALL_MODES, ids=[m.name for m in ALL_MODES])
@pytest.mark.asyncio
async def test_every_mode_emits_the_agent_movement_and_skill_gauges(mode):
    """Live frames set the per-player variance and skill gauges the agent decides on."""
    game = await _started_game(mode, EventCollector())
    with (
        patch("services.game_coordinator.metrics.player_movement_variance") as variance,
        patch("services.game_coordinator.metrics.player_skill_level") as skill,
    ):
        await _feed_motion(game, mode)

    for serial, player in game.players.items():
        assert player.analytics.sample_count == len(MOTION), serial
        variance.labels.assert_any_call(serial=serial, game_id=game.game_id)
        skill.labels.assert_any_call(serial=serial, game_id=game.game_id)


@pytest.mark.uc("UC-041", flow="main", br=["BR-006"])
@pytest.mark.parametrize("mode", ALL_MODES, ids=[m.name for m in ALL_MODES])
@pytest.mark.asyncio
async def test_every_mode_publishes_player_analytics_at_game_end(mode):
    """One ``player_analytics`` summary per player at game end, with the winner flag."""
    events = EventCollector()
    game = await _started_game(mode, events)
    await _feed_motion(game, mode)
    await _end_won_game(game, mode)

    summaries = events.get_events_of_type(GameEvent.PLAYER_ANALYTICS)
    assert sorted(s["serial"] for s in summaries) == sorted(game.players)
    assert all(s["game_id"] == game.game_id for s in summaries)
    assert all(s["sample_count"] == len(MOTION) for s in summaries)
    assert any(s["winner"] for s in summaries)


# Captured from origin/main (c404fef99, before #2358) with MOTION and the fixed clock:
# the legacy modes must publish exactly these summaries after the move into the base.
_GOLDEN_CORE = {
    "mock_controller_0": (1.16, 1.07, 6.42, 66.7, 33.3, "balanced", 1, 0.994),
    "mock_controller_1": (1.19, 1.1, 6.6, 50.0, 50.0, "balanced", 1, 0.994),
    "mock_controller_2": (1.22, 1.13, 6.78, 33.3, 66.7, "active", 2, 0.814),
    "mock_controller_3": (1.25, 1.16, 6.96, 16.7, 83.3, "active", 2, 0.814),
}


def _golden_summary(game, mode: Games, serial: str) -> dict:
    peak, avg, total, still, active, playstyle, playstyle_value, skill = _GOLDEN_CORE[serial]
    summary = {
        "serial": serial,
        "sample_count": 6,
        "duration_ms": 96,
        "peak_accel": peak,
        "peak_accel_time": 0.0,
        "avg_accel": avg,
        "std_accel": 0.062,
        "total_accel": total,
        "zone_still_pct": still,
        "zone_active_pct": active,
        "zone_warning_pct": 0.0,
        "zone_danger_pct": 0.0,
        "near_death_count": 0,
        "warning_count": 0,
        "playstyle": playstyle,
        "playstyle_value": playstyle_value,
        "skill_level": skill,
        "game_id": game.game_id,
    }
    first = next(iter(game.players))  # _make_winner makes the first player (or its team) win
    if mode in (Games.JoustTeams, Games.JoustRandomTeams):
        team = game.players[serial].team
        summary.update(winner=team == game.players[first].team, team=team, survival_time_ms=96)
    elif mode == Games.NonStop:
        summary.update(winner=serial == first, score=100, kills=0, deaths=0)
    else:
        summary.update(winner=serial == first, survival_time_ms=96)
    return summary


@pytest.mark.uc("UC-041", flow="main", br=["BR-006"])
@pytest.mark.parametrize("mode", LEGACY_MODES, ids=[m.name for m in LEGACY_MODES])
@pytest.mark.asyncio
async def test_legacy_modes_publish_unchanged_summaries(mode, fixed_clock):  # noqa: ARG001
    """FFA, Teams, Random Teams and NonStop publish the same payload as before #2358."""
    events = EventCollector()
    game = await _started_game(mode, events)
    await _feed_motion(game, mode)
    await _end_won_game(game, mode)

    summaries = sorted(events.get_events_of_type(GameEvent.PLAYER_ANALYTICS), key=lambda s: s["serial"])
    assert summaries == [_golden_summary(game, mode, serial) for serial in sorted(game.players)]
