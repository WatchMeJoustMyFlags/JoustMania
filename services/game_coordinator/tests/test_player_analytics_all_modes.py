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


@pytest.mark.uc("UC-041", flow="main", br=["BR-006"])
@pytest.mark.asyncio
async def test_traitor_winner_flag_follows_the_secret_team():
    """A traitor wins with its secret team: ``winner`` is by secret team, not the visible team."""
    events = EventCollector()
    game = await _started_game(Games.Traitor, events)
    traitor_serial, loyal_serial, *_ = sorted(game.players)
    traitor, loyal = game.players[traitor_serial], game.players[loyal_serial]
    # The traitor wears the loyal player's colour but wins with the other team.
    traitor.team = loyal.team = 0
    loyal.secret_team = 0
    traitor.secret_team = 1
    traitor.is_traitor = True
    game.traitor_serials = {traitor_serial}
    for serial, player in game.players.items():
        if serial not in (traitor_serial, loyal_serial):
            player.team = player.secret_team = 1
    await _feed_motion(game, Games.Traitor)
    for player in game.players.values():
        player.alive = player.secret_team == 1
    assert await game._check_win_condition()
    game._latch_game_outcome()
    game.running = False
    await game._end_game_impl()

    by_serial = {s["serial"]: s for s in events.get_events_of_type(GameEvent.PLAYER_ANALYTICS)}
    assert by_serial[traitor_serial]["winner"] is True
    assert by_serial[traitor_serial]["team"] == 0  # the visible team, never the secret one
    assert by_serial[loyal_serial]["winner"] is False


def _live_gauge_serials(gauge_mocks: dict, game) -> dict[str, set[str]]:
    """Per gauge name, the serials whose series were last set (labels) minus those removed after."""
    live: dict[str, set[str]] = {}
    for name, gauge in gauge_mocks.items():
        serials: set[str] = set()
        for call in gauge.mock_calls:
            method, args, kwargs = call
            if method == "labels" and kwargs.get("game_id") == game.game_id:
                serials.add(kwargs["serial"])
            elif method == "remove" and args[1:] == (game.game_id,):
                serials.discard(args[0])
        live[name] = serials
    return live


_PLAYER_GAUGES = [
    "player_alive",
    "player_accel_magnitude",
    "player_movement_zone",
    "player_peak_accel",
    "player_playstyle",
    "player_movement_variance",
    "player_skill_level",
    "player_movement_variance_aggregate",
]


async def _fight_frames(game, serials) -> None:
    for frame_index in range(len(MOTION)):
        for serial in serials:
            await game._process_controller_state(_frame(serial, MOTION[frame_index]))


@pytest.mark.uc("UC-041", flow="main", br=["BR-006"])
@pytest.mark.asyncio
async def test_fight_club_waiting_player_exports_no_gauges_until_it_fights_again():
    """A Fight Club combatant back in the queue has no game_player_* series; they return with its next round."""
    game = await _started_game(Games.FightClub, EventCollector())
    game._update_player_colors = AsyncMock()
    game._decide_game_end = lambda: False
    game._round_duration = 1000.0
    gauges = {name: patch(f"services.game_coordinator.metrics.{name}") for name in _PLAYER_GAUGES}
    mocks = {name: p.start() for name, p in gauges.items()}
    try:
        await game._start_round()
        first_pair = (game.current_defender, game.current_fighter)
        for serial in first_pair:
            game.players[serial].invincible_until = 0.0
        await _fight_frames(game, first_pair)
        live = _live_gauge_serials(mocks, game)
        assert all(set(first_pair) <= serials for serials in live.values()), live

        # Time-out: both go back to waiting, and a new pair is seated.
        if game.round_task:
            game.round_task.cancel()
        await game._handle_timeout()
        if game.round_task:
            game.round_task.cancel()
        live = _live_gauge_serials(mocks, game)
        waiting = set(game.queue)
        assert set(first_pair) <= waiting
        assert all(not (serials & waiting) for serials in live.values()), live

        # Those two fight again: their series come back.
        game.queue.remove(first_pair[0])
        game.queue.remove(first_pair[1])
        game.queue[:0] = list(first_pair)
        game.current_defender = game.current_fighter = None
        await game._start_round()
        if game.round_task:
            game.round_task.cancel()
        assert {game.current_defender, game.current_fighter} == set(first_pair)
        for serial in first_pair:
            game.players[serial].invincible_until = 0.0
        await _fight_frames(game, first_pair)
        live = _live_gauge_serials(mocks, game)
        assert all(set(first_pair) <= serials for serials in live.values()), live
    finally:
        for p in gauges.values():
            p.stop()


@pytest.mark.uc("UC-041", flow="main", br=["BR-006"])
@pytest.mark.asyncio
async def test_tournament_match_winner_exports_no_gauges_while_waiting():
    """A Tournament match winner has no game_player_* series while it waits; they return with its next match."""
    game = await _started_game(Games.Tournament, EventCollector())
    game._set_base_color = AsyncMock()
    game._write_effect_guarded = AsyncMock()
    gauges = {name: patch(f"services.game_coordinator.metrics.{name}") for name in _PLAYER_GAUGES}
    mocks = {name: p.start() for name, p in gauges.items()}
    try:
        match = game._get_next_match()
        await game._start_match(match)
        pair = (match.player1_serial, match.player2_serial)
        others = set(game.players) - set(pair)
        live = _live_gauge_serials(mocks, game)
        assert not (live["player_alive"] & others), live  # never-fought players wait too
        for serial in pair:
            game.players[serial].invincible_until = 0.0
        await _fight_frames(game, pair)
        assert all(set(pair) <= serials for serials in _live_gauge_serials(mocks, game).values())

        match.winner_serial = pair[0]
        await game._finalize_match(match)
        live = _live_gauge_serials(mocks, game)
        assert all(pair[0] not in serials for serials in live.values()), live
        assert pair[1] in live["player_alive"]  # the eliminated loser keeps its 0

        # The winner's next match brings its series back.
        next_match = game._get_next_match()
        while next_match is not None and pair[0] not in (next_match.player1_serial, next_match.player2_serial):
            await game._start_match(next_match)
            assert pair[0] not in _live_gauge_serials(mocks, game)["player_alive"]  # still waiting
            next_match.winner_serial = next_match.player1_serial
            await game._finalize_match(next_match)
            next_match = game._get_next_match()
        assert next_match is not None
        await game._start_match(next_match)
        game.players[pair[0]].invincible_until = 0.0
        await _fight_frames(game, (pair[0],))
        assert all(pair[0] in serials for serials in _live_gauge_serials(mocks, game).values())
    finally:
        for p in gauges.values():
            p.stop()
