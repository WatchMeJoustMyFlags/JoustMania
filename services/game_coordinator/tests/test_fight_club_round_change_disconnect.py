"""A combatant's disconnect while the round timer seats the next round (#2025 S4, UC-025 A2 step 2, A4).

On a timeout the round timer task itself seats the next round (``_handle_timeout`` ->
``_start_round``). A disconnect of a newly seated combatant during that round start
forfeits the round (A4 step 4). It must not cancel the task that is still starting
the round: every round that ends (``round_end``) was announced first (``round_start``),
and the game goes on with the next round and its timer.
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_fight_club import _end_round_timer  # noqa: E402
from test_fight_club_disconnect import FC_METRICS, _round_on  # noqa: E402


@pytest.mark.uc("UC-025", flow="A4", steps=[4])
@pytest.mark.uc("UC-025", flow="A2", steps=[2])
@pytest.mark.asyncio
async def test_a_disconnect_while_the_timer_seats_the_next_round_keeps_its_round_start():
    game, events = await _round_on(4, "test_2025_s4", invincible=True, min_rounds=100)
    _end_round_timer(game)
    game.running = True

    seated = asyncio.Event()
    release = asyncio.Event()
    real_colors = game._update_player_colors
    calls = {"n": 0}

    async def colors_held_on_the_next_round_start():
        calls["n"] += 1
        # 1st call: _handle_timeout's colours; 2nd: _start_round of round 2.
        if calls["n"] == 2:
            seated.set()
            await release.wait()
        return await real_colors()

    game._update_player_colors = colors_held_on_the_next_round_start

    with patch(FC_METRICS):
        game.round_end_time = time.time() - 1.0
        timer = asyncio.create_task(game._round_timer())
        game.round_task = timer
        await asyncio.wait_for(seated.wait(), 1.0)
        assert game.round_number == 2
        leaver = game.current_fighter
        # The frame loop's task handles the disconnect while the timer task awaits.
        await game._handle_disconnect(leaver)
        release.set()
        await asyncio.wait_for(asyncio.gather(timer, return_exceptions=True), 2.0)
        await asyncio.sleep(0)

    starts = [e["round"] for e in events.get_events_of_type("round_start")]
    ends = [e["round"] for e in events.get_events_of_type("round_end")]
    assert not timer.cancelled(), "the forfeit cancelled the task that was starting the round"
    assert ends == [2]
    assert starts[:2] == [1, 2], f"round 2 ended without its round_start: starts={starts}"
    assert game.players[leaver].alive is False
    # The game went on: round 3 is seated with a live timer.
    assert game.round_number == 3
    assert starts[-1] == 3
    assert game.round_task is not None and not game.round_task.done()
    _end_round_timer(game)
