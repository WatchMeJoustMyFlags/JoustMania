"""Exactly one ``player_death`` span event per death, in every mode (#1327).

Dry run #2 (trace c8e9aa27347f60d5a5ac1649e8dd4233) showed two ``player_death``
events per death on the FFA player span: one from the base kill path and a
poorer one from ``FFAGame._kill_player_impl``. Teams, Traitor, Werewolf and
NonStop had the same duplicate. Spec: docs/specs/telemetry.md §3.2.
"""

import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from opentelemetry import trace

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import MockControllerManagerService, async_noop

from lib.types import Games
from services.game_coordinator.game_factory import _GAME_CLASSES


class _RecordingStream:
    """Gameplay stream stand-in: accepts and drops every write."""

    async def write(self, message):  # noqa: ARG002
        return None


def _autospec_span():
    span = create_autospec(trace.Span, instance=True)
    span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
    return span


async def _game_with_spans(mode: Games, num_players: int = 4):
    """A real game of ``mode`` with initialized players, names and autospec'd spans."""
    mock_cm = MockControllerManagerService(num_controllers=num_players)
    game = _GAME_CLASSES[mode](
        controller_manager_client=mock_cm,
        event_publisher=async_noop,
        audio_client=None,
        game_id=f"test_death_event_{mode.name.lower()}",
    )
    game.random_teams = False
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = _RecordingStream()
    game.running = True  # kills happen in a running game (Swapper declines them otherwise, #1988)
    spans = {}
    for i, (serial, player) in enumerate(game.players.items()):
        player.name = f"Player {i}"
        player.span = _autospec_span()
        spans[serial] = player.span
    if mode == Games.Tournament:
        _seat_first_player_in_a_match(game)
    return game, spans


def _seat_first_player_in_a_match(game) -> None:
    """Tournament takes a kill only from a fighter of the undecided current match (UC-025 BR-006)."""
    from services.game_coordinator.games.tournament import TournamentState

    serial = next(iter(game.players))
    match = next(m for m in game.bracket if serial in (m.player1_serial, m.player2_serial) and not m.is_complete)
    game.current_match = match
    for fighter in (match.player1_serial, match.player2_serial):
        game.players[fighter].tournament_state = TournamentState.FIGHTING


def _death_events(span):
    return [c.kwargs["attributes"] for c in span.add_event.call_args_list if c.args and c.args[0] == "player_death"]


ALL_MODES = list(_GAME_CLASSES)


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.parametrize("mode", ALL_MODES, ids=[m.name for m in ALL_MODES])
async def test_one_player_death_event_per_death(mode):
    """UC-030 step 9: a death records exactly one player_death on the player span."""
    game, spans = await _game_with_spans(mode)
    serial = next(iter(game.players))

    await game._kill_player(serial, accel_mag=3.0)

    deaths = _death_events(spans[serial])
    assert len(deaths) == 1, f"{mode.name}: {spans[serial].add_event.call_args_list}"
    attrs = deaths[0]
    assert attrs["player.serial"] == serial
    assert attrs["player.name"] == "Player 0"
    assert attrs["death.reason"] == "motion"
    assert attrs["accel_magnitude"] == 3.0
    # No other player's span got a player_death.
    for other, span in spans.items():
        if other != serial:
            assert _death_events(span) == [], f"{mode.name}: {other}"


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[9])
async def test_ffa_two_deaths_two_events_in_order():
    """UC-030 step 9: two FFA deaths give exactly two events, alive_remaining counting down."""
    game, spans = await _game_with_spans(Games.JoustFFA, num_players=6)
    first, second = list(game.players)[:2]

    await game._kill_player(first, accel_mag=3.0)
    await game._kill_player(second, accel_mag=3.0)

    assert [a["alive_remaining"] for a in _death_events(spans[first])] == [5]
    assert [a["alive_remaining"] for a in _death_events(spans[second])] == [4]


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[9])
async def test_dead_player_killed_again_records_nothing():
    """UC-030 step 9: a second kill of an already-dead player adds no player_death."""
    game, spans = await _game_with_spans(Games.JoustFFA)
    serial = next(iter(game.players))
    await game._kill_player(serial, accel_mag=3.0)
    await game._kill_player(serial, accel_mag=3.0)
    assert len(_death_events(spans[serial])) == 1


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[9])
async def test_agent_elimination_carries_reason():
    """UC-030 step 9 / UC-040 eliminate_player: the one event carries death.reason=agent_intervention."""
    game, spans = await _game_with_spans(Games.JoustFFA)
    serial = next(iter(game.players))
    await game._kill_player(serial, accel_mag=0.0, reason="agent_intervention")
    (attrs,) = _death_events(spans[serial])
    assert attrs["death.reason"] == "agent_intervention"


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[9])
async def test_teams_extras_on_the_single_event():
    """UC-030 step 9: Teams adds team / team_eliminated to the base event."""
    game, spans = await _game_with_spans(Games.JoustTeams, num_players=4)
    by_team: dict[int, list[str]] = {}
    for serial, player in game.players.items():
        by_team.setdefault(player.team, []).append(serial)
    team, members = next((t, m) for t, m in by_team.items() if len(m) >= 2)

    await game._kill_player(members[0], accel_mag=3.0)
    (first,) = _death_events(spans[members[0]])
    assert first["team"] == team
    assert first["team_eliminated"] is False

    for serial in members[1:]:
        await game._kill_player(serial, accel_mag=3.0)
    (last,) = _death_events(spans[members[-1]])
    assert last["team_eliminated"] is True


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[9])
async def test_traitor_extras_on_the_single_event():
    """UC-030 step 9: Traitor adds is_traitor / visible_team / secret_team to the base event."""
    game, spans = await _game_with_spans(Games.Traitor, num_players=4)
    serial = next(iter(game.players))
    player = game.players[serial]
    await game._kill_player(serial, accel_mag=3.0)
    (attrs,) = _death_events(spans[serial])
    assert attrs["is_traitor"] == player.is_traitor
    assert attrs["visible_team"] == player.team
    assert attrs["secret_team"] == player.secret_team
    assert "team_eliminated" in attrs


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[9])
async def test_werewolf_extras_on_the_single_event():
    """UC-030 step 9: Werewolf adds is_werewolf / was_revealed to the base event."""
    game, spans = await _game_with_spans(Games.Werewolf, num_players=4)
    serial = next(iter(game.players))
    await game._kill_player(serial, accel_mag=3.0)
    (attrs,) = _death_events(spans[serial])
    assert attrs["is_werewolf"] == game.players[serial].is_werewolf
    assert attrs["was_revealed"] == game.players[serial].revealed


@pytest.mark.asyncio
@pytest.mark.uc("UC-030", flow="main", steps=[9])
async def test_nonstop_extras_count_this_death_and_span_stays_open():
    """UC-030 step 9: NonStop's event counts this death; the span stays open for the respawn."""
    game, spans = await _game_with_spans(Games.NonStop, num_players=2)
    serial = next(iter(game.players))
    await game._kill_player(serial, accel_mag=3.0)
    (attrs,) = _death_events(spans[serial])
    assert attrs["deaths"] == 1 == game.players[serial].deaths
    assert attrs["kills"] == 0
    spans[serial].end.assert_not_called()
