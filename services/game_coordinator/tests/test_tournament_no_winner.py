"""Tournament `tournament_no_winner` (UC-025 A3, BR-003; maintainer decision M2, #1337).

Published once, only when the bracket finishes with nobody left (every player out
of it, e.g. all disconnected). The outcome is latched in the same synchronous step
with no champion. A force-end never publishes it: it only gets `game_ended` with
champion `none`, `outcome` `force_ended` and a reason.
"""

import sys
from pathlib import Path

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_tournament_run import _bracket_game, _frame

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator.games.tournament import TournamentState


async def _drive(game, frames=50, first=None):
    """Feed frames (the first one given) until the game loop would end."""
    for index in range(frames):
        update = first if index == 0 and first is not None else _frame()
        if await game._process_gameplay_update(update):
            return True
    return False


@pytest.mark.uc("UC-025", flow="A3", steps=[1, 2], br=["BR-003"])
@pytest.mark.asyncio
async def test_all_players_disconnecting_publishes_one_no_winner_and_no_champion():
    game, events = await _bracket_game(num_players=3)

    finished = await _drive(game, first=_frame(disconnects=list(game.players)))

    assert finished
    assert all(p.tournament_state == TournamentState.ELIMINATED for p in game.players.values())
    assert len(events.get_events_of_type("tournament_no_winner")) == 1
    assert events.get_events_of_type("tournament_champion") == []
    assert game._champion is None
    assert game.game_outcome() == "completed"

    writes_before_end = len(game.gameplay_stream.messages)
    await game._end_game_impl()
    ended = events.get_events_of_type(GameEvent.GAME_ENDED)
    assert len(ended) == 1
    assert ended[0]["champion"] == "none"
    assert ended[0]["outcome"] == "completed"
    assert len(events.get_events_of_type("tournament_no_winner")) == 1
    assert Sound.VOX_CONGRATULATIONS not in [c.args[0] for c in game._play_sound.call_args_list]
    assert not any(
        m.HasField("game_effect") and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW
        for m in game.gameplay_stream.messages[writes_before_end:]
    ), "the end of a no-winner game showed a champion rainbow"


@pytest.mark.uc("UC-025", flow="A3", steps=[1], br=["BR-003"])
@pytest.mark.asyncio
async def test_a_force_end_publishes_no_no_winner():
    game, events = await _bracket_game(num_players=3)
    game.force_end("operator")

    await game._end_game_impl()

    assert events.get_events_of_type("tournament_no_winner") == []
    ended = events.get_events_of_type(GameEvent.GAME_ENDED)[0]
    assert ended["champion"] == "none"
    assert ended["outcome"] == "force_ended"
    assert ended["reason"] == "operator"


@pytest.mark.uc("UC-025", flow="A3", steps=[1], br=["BR-003"])
@pytest.mark.asyncio
async def test_a_force_end_after_the_bracket_emptied_before_the_tick_publishes_nothing():
    """The bracket step runs after a force-end: not running, so nothing is latched or published."""
    game, events = await _bracket_game(num_players=3)
    await game._process_gameplay_update(_frame(disconnects=list(game.players)))
    game.force_end("operator")

    await _drive(game)

    assert events.get_events_of_type("tournament_no_winner") == []
    assert game.game_outcome() == "force_ended"


@pytest.mark.uc("UC-025", flow="A3", steps=[1], br=["BR-003"])
@pytest.mark.asyncio
async def test_a_force_end_inside_the_latch_window_publishes_no_no_winner():
    """The force-end lands between the running check and the outcome latch (#2002 recipe)."""
    game, events = await _bracket_game(num_players=3)
    real_latch = type(game)._latch_game_outcome
    fired: list = []

    def latch():
        if not fired:
            fired.append(True)
            game.force_end("latch_window")
        real_latch(game)

    game._latch_game_outcome = latch
    await _drive(game, first=_frame(disconnects=list(game.players)))

    assert fired, "the latch window was never hit"
    assert events.get_events_of_type("tournament_no_winner") == []
    assert game.game_outcome() == "force_ended"
    assert game._no_winner_latched is False
