"""A Fight Club round loss is a death, not an elimination (UC-025, UC-041 BR-008; #2517).

The loser stays in the game, so the kill path sets no ``game_player_elimination_order``
and its ``player_death`` span event carries ``death.eliminated`` false. A player who
leaves the rotation for good (a disconnect, A4) is eliminated: it gets its order
once, when it leaves, and its ``player_death`` carries ``death.eliminated`` true.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_fight_club import (  # noqa: E402
    RecordingGameplayStream,
    _clear_invincibility,
    _end_round_timer,
    _make_game,
    _no_pause,
)

from services.game_coordinator.games.fight_club import FightClubGame  # noqa: E402

FC_METRICS = "services.game_coordinator.games.fight_club.metrics"
BASE_METRICS = "services.game_coordinator.games.base.metrics"


async def _seated_game(num_players: int, game_id: str) -> FightClubGame:
    game, mock_cm, _ = _make_game(num_players, game_id)
    game._min_rounds = 100
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = RecordingGameplayStream()
    game._pause_between_rounds = _no_pause
    game.running = True
    for player in game.players.values():
        player.span = MagicMock()
    await game._start_round()
    _clear_invincibility(game)
    return game


def _death_events(game: FightClubGame, serial: str) -> list[dict]:
    span = game.players[serial].span
    return [c.kwargs["attributes"] for c in span.add_event.call_args_list if c.args and c.args[0] == "player_death"]


def _orders(base_metrics) -> list[tuple[str, int]]:
    gauge = base_metrics.player_elimination_order
    sets = gauge.labels.return_value.set.call_args_list
    return [
        (labels.kwargs["serial"], set_call.args[0])
        for labels, set_call in zip(gauge.labels.call_args_list, sets, strict=True)
    ]


class TestFightClubRoundLossIsNoElimination:
    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.uc("UC-025", br=["BR-006"])
    @pytest.mark.asyncio
    async def test_every_player_losing_a_round_records_no_elimination_order(self):
        """4 players, every one loses a round: four deaths, no elimination order, ``death.eliminated`` false."""
        game = await _seated_game(4, "test_fc_round_loss_no_elim")
        losers: list[str] = []

        with patch(FC_METRICS), patch(BASE_METRICS) as base_metrics:
            while len(set(losers)) < 4:
                loser = game.current_fighter
                assert await game._kill_player(loser, 5.0) is True
                losers.append(loser)
                _clear_invincibility(game)
                if len(set(losers)) == 3:
                    # The unbeaten defender loses the next round.
                    game.current_defender, game.current_fighter = game.current_fighter, game.current_defender

            base_metrics.player_elimination_order.labels.assert_not_called()

        assert set(losers) == set(game.players)
        assert game.dead_count == len(losers)
        assert game._elimination_count == 0
        for serial in game.players:
            events = _death_events(game, serial)
            assert events, serial
            assert all(e["death.eliminated"] is False for e in events)
            assert game.players[serial].alive is True
        _end_round_timer(game)

    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.uc("UC-025", flow="A4", br=["BR-006"])
    @pytest.mark.asyncio
    async def test_a_player_who_leaves_the_rotation_is_eliminated_once(self):
        """A round loser that disconnects while waiting gets order 1, once; that death says eliminated."""
        game = await _seated_game(4, "test_fc_leave_is_elim")
        loser = game.current_fighter

        with patch(FC_METRICS), patch(BASE_METRICS) as base_metrics:
            assert await game._kill_player(loser, 5.0) is True
            assert loser in game.queue
            await game._handle_disconnect(loser)

            assert _orders(base_metrics) == [(loser, 1)]

        assert game._elimination_count == 1
        assert loser not in game.queue
        assert game.players[loser].alive is False
        assert [e["death.eliminated"] for e in _death_events(game, loser)] == [False, True]
        _end_round_timer(game)

    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.uc("UC-025", flow="A4", br=["BR-006"])
    @pytest.mark.asyncio
    async def test_a_combatant_who_disconnects_forfeits_and_is_eliminated(self):
        """A seated fighter disconnects: it forfeits the round and is the first player out (order 1)."""
        game = await _seated_game(3, "test_fc_forfeit_is_elim")
        fighter, defender = game.current_fighter, game.current_defender

        with patch(FC_METRICS), patch(BASE_METRICS) as base_metrics:
            await game._handle_disconnect(fighter)

            assert _orders(base_metrics) == [(fighter, 1)]

        assert game.players[defender].score == 1
        assert [e["death.eliminated"] for e in _death_events(game, fighter)] == [True]
        _end_round_timer(game)

    @pytest.mark.uc("UC-041", br=["BR-008"])
    @pytest.mark.uc("UC-025", flow="A4", steps=[5], br=["BR-006"])
    @pytest.mark.asyncio
    async def test_a_round_loser_leaving_during_the_round_change_is_eliminated_once(self):
        """The loser disconnects in the pause, its loss still finishing: order 1 once, no second death."""
        game = await _seated_game(3, "test_fc_leave_in_pause")
        loser = game.current_fighter
        paused, release = asyncio.Event(), asyncio.Event()

        async def held_pause():
            paused.set()
            await release.wait()

        game._pause_between_rounds = held_pause

        with patch(FC_METRICS), patch(BASE_METRICS) as base_metrics:
            kill = asyncio.create_task(game._kill_player(loser, 5.0))
            await asyncio.wait_for(paused.wait(), 1.0)
            assert loser in game._finishing_round_loss
            await game._handle_disconnect(loser)
            assert _orders(base_metrics) == [(loser, 1)]
            release.set()
            assert await kill is True

            assert _orders(base_metrics) == [(loser, 1)]

        assert game.dead_count == 1
        assert game._elimination_count == 1
        assert loser not in game.queue
        # The round loss is the only death: no player_death says eliminated on this path.
        assert [e["death.eliminated"] for e in _death_events(game, loser)] == [False]
        _end_round_timer(game)
