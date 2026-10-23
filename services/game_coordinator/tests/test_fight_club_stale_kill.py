"""Fight Club kills that cross a round change, with invincibility 0 (UC-025 A4, BR-006; #2015).

With ``invincibility_seconds`` 0 a seated combatant is out of its grace at once, so
grace no longer covers two races: a kill that entered during an earlier round (still
awaiting its explosion) must not decide the next round, and a re-seated round loser
whose disconnect waits for its round-loss kill must not win the seated round.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_fight_club import _clear_invincibility, _end_round_timer  # noqa: E402
from test_fight_club_disconnect import (  # noqa: E402
    FC_METRICS,
    _counted,
    _eliminate_ctx,
    _GatedExplosion,
    _round_on,
)

from services.game_coordinator.lifecycle_handlers import handle_eliminate_player  # noqa: E402


async def _round_without_invincibility(num_players: int, game_id: str):
    game, events = await _round_on(num_players, game_id, invincible=True, min_rounds=100)
    game._invincibility_duration = 0.0
    _clear_invincibility(game)
    return game, events


class TestFightClubKillAcrossRounds:
    """UC-025 BR-006 (#2015): a kill belongs to the round it entered."""

    @pytest.mark.uc("UC-025", flow="A2", steps=[2], br=["BR-006"])
    @pytest.mark.asyncio
    async def test_a_kill_from_a_timed_out_round_does_not_land_in_the_next_round(self):
        """Two players: the defender's kill awaits its explosion, the round times out and the
        same pair is seated again (out of grace at once); the stale kill is declined."""
        game, events = await _round_without_invincibility(2, "test_fc_stale_kill")
        explosion = _GatedExplosion()
        game._play_sound = explosion
        defender = game.current_defender
        counted = _counted(game)

        with patch(FC_METRICS):
            kill = asyncio.create_task(game._kill_player(defender, 5.0))
            await asyncio.wait_for(explosion.started.wait(), 1.0)
            await game._handle_timeout()
            assert defender in (game.current_defender, game.current_fighter)
            assert not game.is_in_grace(game.players[defender])
            explosion.release.set()
            assert await kill is False

        assert _counted(game) == counted
        assert all(p.score == 0 for p in game.players.values())
        assert game.players[defender].alive is True
        assert not events.get_events_of_type("round_end")
        # The same player is killable in the new round.
        assert game._accepts_kill(defender) is True
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[4, 5], br=["BR-006"])
    @pytest.mark.asyncio
    async def test_a_seated_loser_whose_disconnect_waits_cannot_win_the_seated_round(self):
        """Two players: the fighter loses, is seated again and disconnects while the next
        ``round_start`` is published; the defender's death in that window is declined, and the
        deferred forfeit gives the defender the round."""
        game, events = await _round_without_invincibility(2, "test_fc_deferred_dc_race")
        defender, loser = game.current_defender, game.current_fighter
        round_start_published, round_start_release = asyncio.Event(), asyncio.Event()
        publish = game.event_publisher

        async def held_round_start(event_type, data):
            await publish(event_type, data)
            if event_type == "round_start":
                round_start_published.set()
                await round_start_release.wait()

        game.event_publisher = held_round_start

        with patch(FC_METRICS):
            kill = asyncio.create_task(handle_eliminate_player(_eliminate_ctx(game, loser)))
            await asyncio.wait_for(round_start_published.wait(), 1.0)
            assert game.current_fighter == loser
            _clear_invincibility(game)
            await game._handle_disconnect(loser)
            assert loser in game._deferred_disconnects
            assert await game._kill_player(defender, 5.0) is False
            round_start_release.set()
            await kill

        assert game.players[loser].score == 0
        assert game.players[defender].score == 2
        assert [e["winner"] for e in events.get_events_of_type("round_end")] == [defender, defender]
        assert game.players[loser].alive is False
        assert game._latched_winner == defender
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[5, 6], br=["BR-005", "BR-006"])
    @pytest.mark.asyncio
    async def test_both_combatants_disconnect_while_the_forfeit_waits_the_forfeit_comes_first(self):
        """Two players: the re-seated loser's disconnect waits, then the defender disconnects
        too; the loser's forfeit applies first (the defender wins 2-0), and the defender's
        leave, handled after it, finds the game ended with the defender crowned."""
        game, events = await _round_without_invincibility(2, "test_fc_double_dc")
        defender, loser = game.current_defender, game.current_fighter
        round_start_published, round_start_release = asyncio.Event(), asyncio.Event()
        publish = game.event_publisher

        async def held_round_start(event_type, data):
            await publish(event_type, data)
            if event_type == "round_start":
                round_start_published.set()
                await round_start_release.wait()

        game.event_publisher = held_round_start

        with patch(FC_METRICS):
            kill = asyncio.create_task(handle_eliminate_player(_eliminate_ctx(game, loser)))
            await asyncio.wait_for(round_start_published.wait(), 1.0)
            assert game.current_fighter == loser
            await game._handle_disconnect(loser)
            assert loser in game._deferred_disconnects
            await game._handle_disconnect(defender)
            assert game.players[defender].score == 1
            assert game._accepts_kill(defender) is False
            round_start_release.set()
            await kill

        assert game.players[loser].score == 0
        assert game.players[defender].score == 2
        assert [e["winner"] for e in events.get_events_of_type("round_end")] == [defender, defender]
        assert game.players[loser].alive is False
        assert game._latched_winner == defender
        assert not game._disconnects_after_forfeit
        _end_round_timer(game)
