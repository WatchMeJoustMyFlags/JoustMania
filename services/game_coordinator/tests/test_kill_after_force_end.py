"""No kill counts once the game is force-ended, in any mode (UC-027 step 3, UC-030 step 9; #2033).

A force-end runs on the servicer thread and can land while the game thread is still
in a frame. Before #2033 FFA's win check returned False on ``force_ended`` without
latching, so the rest of that frame's controllers still went through the kill path:
``dead_count``, ``player_death``, the death effect and a mode's score counted until
the next frame saw ``running`` False. The base kill path now declines every kill
once the game is force-ended, at its first and at its second gate check.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_fight_club import _end_round_timer  # noqa: E402
from test_fight_club_disconnect import FC_METRICS, _counted, _round_on  # noqa: E402
from test_kill_after_latch import (  # noqa: E402
    DEFAULT_GATE_MODES,
    _assert_nothing_counted,
    _game,
    _stub_mode_handler,
)
from test_tournament_run import _bracket_game, _frame  # noqa: E402

from lib.types import GameEvent, Sound  # noqa: E402
from services.game_coordinator.games.ffa import FFAGame  # noqa: E402
from services.game_coordinator.games.swapper import SwapperGame  # noqa: E402

BASE_METRICS = "services.game_coordinator.games.base.metrics"


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.uc("UC-020", br=["BR-003"])
@pytest.mark.asyncio
async def test_ffa_kills_later_in_the_frame_of_a_force_end_count_nothing():
    """The issue's case: the force-end lands during the frame's first kill (its explosion);
    neither that kill nor the next controller's kill in the same frame counts."""
    game, events = _game(FFAGame, ("p1", "p2", "p3"))

    async def force_end_on_explosion(sound, *_args, **_kwargs):
        game.sounds.append(sound)
        if sound == Sound.SFX_EXPLOSION:
            game.force_end("operator")

    game._play_sound = force_end_on_explosion

    with patch(BASE_METRICS) as m:
        game_over = await game._process_gameplay_update(_frame(("p1", "p2"), spike=("p1", "p2")))

    assert game_over is False
    assert game.sounds.count(Sound.SFX_EXPLOSION) == 1  # only the kill already under way
    game.sounds.remove(Sound.SFX_EXPLOSION)
    for serial in ("p1", "p2"):
        _assert_nothing_counted(game, events, serial, m)
    assert events.get_events_of_type(GameEvent.GAME_WINNER) == []
    assert events.get_events_of_type(GameEvent.GAME_TIE) == []
    assert game.game_outcome() == "force_ended"


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.parametrize("game_cls", [*DEFAULT_GATE_MODES, SwapperGame], ids=lambda c: c.__name__)
@pytest.mark.asyncio
async def test_a_kill_after_a_force_end_is_declined(game_cls):
    """First check: a kill entering after the force-end is declined before any bookkeeping."""
    game, events = _game(game_cls)
    _stub_mode_handler(game)
    game.force_end("operator")
    game.players["p1"].shield_charge_seconds = 5.0

    with patch(BASE_METRICS) as m:
        assert await game._kill_player("p1", 3.0) is False

    assert game.players["p1"].shield_charge_seconds == 5.0  # declined ahead of the charge discard
    _assert_nothing_counted(game, events, "p1", m)


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.uc("UC-030", flow="main", steps=[9])
@pytest.mark.parametrize("game_cls", DEFAULT_GATE_MODES, ids=lambda c: c.__name__)
@pytest.mark.asyncio
async def test_a_kill_whose_explosion_spans_the_force_end_is_declined(game_cls):
    """Second check: the force-end lands while the kill awaits its explosion."""
    game, events = _game(game_cls)
    _stub_mode_handler(game)

    async def force_end_on_explosion(sound, *_args, **_kwargs):
        game.sounds.append(sound)
        if sound == Sound.SFX_EXPLOSION:
            game.force_end("operator")

    game._play_sound = force_end_on_explosion

    with patch(BASE_METRICS) as m:
        assert await game._kill_player("p1", 3.0) is False

    game.sounds.remove(Sound.SFX_EXPLOSION)  # the explosion has played (UC-030 step 9)
    _assert_nothing_counted(game, events, "p1", m)


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.uc("UC-025", br=["BR-006"])
@pytest.mark.asyncio
async def test_fight_club_round_is_not_decided_after_a_force_end():
    game, events = await _round_on(2, "test_fc_force_end_kill")
    defender = game.current_defender
    assert game._accepts_kill(defender) is True
    counted = _counted(game)
    game.force_end("operator")

    with patch(FC_METRICS), patch(BASE_METRICS):
        assert await game._kill_player(defender, 5.0) is False

    assert _counted(game) == counted
    assert all(p.score == 0 for p in game.players.values())
    assert game.players[defender].alive is True
    assert not events.get_events_of_type("round_end")
    _end_round_timer(game)


@pytest.mark.uc("UC-027", flow="main", steps=[3])
@pytest.mark.uc("UC-025", br=["BR-006"])
@pytest.mark.asyncio
async def test_tournament_match_is_not_decided_after_a_force_end():
    game, events = await _bracket_game(num_players=2)
    fighter = game.current_match.player1_serial
    assert game._accepts_kill(fighter) is True
    game.force_end("operator")

    with patch(BASE_METRICS):
        assert await game._kill_player(fighter, 5.0) is False

    assert game.dead_count == 0
    assert game.players[fighter].alive is True
    assert not events.get_events_of_type("match_end")
