"""Tournament kill gate and interventions (UC-025 step 3, A4, BR-004, BR-005, BR-006; #1383, #1737, D-G8, D-G34).

Tournament's ``_accepts_kill`` takes a kill only for a fighter of the undecided
current match out of its grace, or for a disconnected player, and none once the
champion is latched. Every other kill is declined before the base death
accounting, so an ``eliminate_player`` intervention on such a player is refused
with ``eliminate_refused`` and counts nothing.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc
import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from test_tournament_champion import _ended, _events, _game, _LethalStream, _run
from test_tournament_run import _bracket_game, _frame

from lib.types import GameEvent, Sound
from proto import controller_manager_pb2
from services.game_coordinator import game_clock
from services.game_coordinator.games import tournament as tournament_mod
from services.game_coordinator.games.tournament import TournamentState
from services.game_coordinator.intervention_errors import InterventionNotApplied
from services.game_coordinator.interventions import (
    BLOCK_ELIMINATE_REFUSED,
    INTERVENTION_SPECS,
    MODE_CAPABILITY_DENY,
    InterventionContext,
)
from services.game_coordinator.lifecycle_handlers import handle_eliminate_player

DEATH = controller_manager_pb2.GAME_EFFECT_PLAYER_DEATH
ELIMINATE_SPEC = next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player")


def _eliminate_ctx(game, serial) -> InterventionContext:
    return InterventionContext(
        spec=ELIMINATE_SPEC,
        value=serial,
        payload=serial,
        target_serial=serial,
        game=game,
        objective="balanced",
    )


def _death_effects(game) -> list[str]:
    return [
        m.game_effect.serial
        for m in game.gameplay_stream.messages
        if m.HasField("game_effect") and m.game_effect.effect == DEATH
    ]


def _explosions(game) -> int:
    return sum(1 for call in game._play_sound.call_args_list if call.args and call.args[0] == Sound.SFX_EXPLOSION)


def _gauge_zero_serials(metrics_mock) -> list[str]:
    """Serials whose ``game_player_alive`` gauge the base kill path set to 0."""
    return [call.kwargs.get("serial") for call in metrics_mock.player_alive.labels.call_args_list]


async def _state_waiting(game):
    match = game.current_match
    return next(s for s in game.players if s not in (match.player1_serial, match.player2_serial))


async def _state_bye(game):
    """3 players: the bye player waits in round 2 from the start (#1990)."""
    match = game.current_match
    return next(s for s in game.players if s not in (match.player1_serial, match.player2_serial))


async def _state_in_grace(game):
    return game.current_match.player1_serial


async def _state_decided_match_winner(game):
    """The match is decided (an intervention outside a frame) but not finalised yet."""
    match = game.current_match
    assert await game._kill_player(match.player1_serial, 0.0, reason="agent_intervention") is True
    assert match.is_complete and game.current_match is match
    return match.player2_serial


async def _state_between_matches(game):
    """The first match is finalised; the next one waits for the pause between matches."""
    match = game.current_match
    game._time_between_matches = 60.0
    await game._process_gameplay_update(_frame([match.player1_serial], spike=[match.player1_serial]))
    assert game.current_match is None and match.is_complete
    return match.player2_serial


async def _state_after_the_latch(game):
    final = game.current_match
    assert await game._process_gameplay_update(_frame([final.player1_serial], spike=[final.player1_serial])) is True
    assert game._champion == final.player2_serial
    return final.player2_serial


REFUSED_CASES = [
    pytest.param(4, 0.0, _state_waiting, id="waiting-player"),
    pytest.param(3, 0.0, _state_bye, id="bye-player"),
    pytest.param(4, 60.0, _state_in_grace, id="fighter-in-grace"),
    pytest.param(4, 0.0, _state_decided_match_winner, id="winner-of-decided-match"),
    pytest.param(4, 0.0, _state_between_matches, id="between-matches"),
    pytest.param(2, 0.0, _state_after_the_latch, id="after-champion-latch"),
]


@pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-006"])
@pytest.mark.uc("UC-040", flow="A22")
@pytest.mark.parametrize(("num_players", "invincibility_s", "arrange"), REFUSED_CASES)
@pytest.mark.asyncio
async def test_eliminate_on_a_player_that_cannot_lose_is_refused_and_counts_nothing(
    num_players, invincibility_s, arrange
):
    game, events = await _bracket_game(num_players=num_players, invincibility_s=invincibility_s)
    target = await arrange(game)
    state_before = game.players[target].tournament_state
    dead_before, order_before = game.dead_count, game._elimination_count
    explosions_before = _explosions(game)
    match_ends_before = len(events.get_events_of_type("match_end"))

    assert game._accepts_kill(target) is False
    with (
        patch("services.game_coordinator.games.base.metrics") as m,
        pytest.raises(InterventionNotApplied) as exc,
    ):
        await handle_eliminate_player(_eliminate_ctx(game, target))

    assert exc.value.reason == BLOCK_ELIMINATE_REFUSED
    player = game.players[target]
    assert player.alive is True
    assert player.tournament_state == state_before
    assert game.dead_count == dead_before
    assert game._elimination_count == order_before
    m.player_alive.labels.assert_not_called()
    m.player_elimination_order.labels.assert_not_called()
    m.player_deaths_total.labels.assert_not_called()
    assert _explosions(game) == explosions_before, "a declined kill played the explosion"
    assert target not in _death_effects(game)
    assert len(events.get_events_of_type("match_end")) == match_ends_before


@pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-006"])
@pytest.mark.asyncio
async def test_a_waiting_players_lethal_spike_changes_no_bookkeeping():
    game, _ = await _bracket_game()
    waiting = await _state_waiting(game)

    with patch("services.game_coordinator.games.base.metrics") as m:
        for _ in range(10):
            await game._process_gameplay_update(_frame([waiting], spike=[waiting]))
        # The kill gate holds even when a kill reaches the base path from elsewhere.
        assert await game._kill_player(waiting, 9.0) is False

    assert game.players[waiting].alive and game.players[waiting].tournament_state == TournamentState.WAITING
    assert game.dead_count == 0 and game._elimination_count == 0
    m.player_alive.labels.assert_not_called()
    m.player_deaths_total.labels.assert_not_called()
    assert _explosions(game) == 0


@pytest.mark.uc("UC-025", flow="main", steps=[3, 4], br=["BR-006"])
@pytest.mark.uc("UC-040", flow="A22")
@pytest.mark.asyncio
async def test_eliminate_on_a_fighter_loses_it_the_match():
    game, events = await _bracket_game()
    match = game.current_match
    loser, winner = match.player1_serial, match.player2_serial

    with patch("services.game_coordinator.games.base.metrics") as m:
        await handle_eliminate_player(_eliminate_ctx(game, loser))

    assert match.is_complete and match.winner_serial == winner
    assert game.players[loser].alive is False
    assert game.players[loser].tournament_state == TournamentState.ELIMINATED
    assert game.dead_count == 1
    assert _gauge_zero_serials(m) == [loser]
    m.player_deaths_total.labels.return_value.inc.assert_called_once()
    assert _death_effects(game) == [loser]

    # The next bracket step finalises the match: match_end, the winner advances.
    await game._process_gameplay_update(_frame())
    assert [(e["winner"], e["loser"]) for e in events.get_events_of_type("match_end")] == [(winner, loser)]
    assert game.players[winner].round_number == 2


@pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-004", "BR-006"])
@pytest.mark.asyncio
async def test_the_double_hit_winner_keeps_its_alive_gauge():
    game, _ = await _bracket_game()
    match = game.current_match
    first, second = match.player1_serial, match.player2_serial

    with patch("services.game_coordinator.games.base.metrics") as m:
        await game._process_gameplay_update(_frame([first, second], spike=[first, second]))

    assert match.winner_serial == second
    assert game.players[second].alive is True
    assert game.dead_count == 1 and game._elimination_count == 1
    assert _gauge_zero_serials(m) == [first], "the double-hit winner's gauge was set to 0"
    m.player_deaths_total.labels.return_value.inc.assert_called_once()
    assert _explosions(game) == 1


@pytest.mark.uc("UC-025", flow="A4", steps=[1, 2, 3], br=["BR-006"])
@pytest.mark.parametrize("num_players", [3, 5, 6, 8])
@pytest.mark.asyncio
async def test_brackets_with_disconnects_and_interventions_complete(num_players):
    """A waiting player and later a fighter disconnect; refused eliminates on waiting players count nothing."""
    game, events = await _bracket_game(num_players=num_players)
    first = game.current_match
    waiting_leaver = await _state_waiting(game)

    finished = False
    fighter_left = False
    with patch.object(tournament_mod, "ROUND_PAUSE_S", 0.0):
        await game._process_gameplay_update(_frame(disconnects=[waiting_leaver]))
        for _ in range(3000):
            match = game.current_match
            update = _frame()
            if match is not None and not match.is_complete:
                # An eliminate on a waiting player is refused every time.
                waiting = [
                    s for s, p in game.players.items() if p.alive and p.tournament_state == TournamentState.WAITING
                ]
                for serial in waiting:
                    with pytest.raises(InterventionNotApplied):
                        await handle_eliminate_player(_eliminate_ctx(game, serial))
                if not fighter_left and match is not first:
                    fighter_left = True
                    update = _frame(disconnects=[match.player2_serial])
                else:
                    await handle_eliminate_player(_eliminate_ctx(game, match.player1_serial))
            if await game._process_gameplay_update(update):
                finished = True
                break

    left = [s for s, p in game.players.items() if p.tournament_state != TournamentState.ELIMINATED]
    assert finished, f"the bracket stalled with {len(left)} players left"
    assert len(left) == 1 and game._champion == left[0]
    assert not game._stall_logged
    # Every player but the champion was counted out exactly once.
    assert game.dead_count == num_players - 1
    assert game._elimination_count == num_players - 1
    announced = events.get_events_of_type("tournament_champion")
    assert [(e["champion"], e["rounds_played"]) for e in announced] == [(left[0], game.total_rounds)]


@pytest.mark.uc("UC-025", flow="A4", steps=[2], br=["BR-005"])
@pytest.mark.asyncio
async def test_a_walkover_final_reports_the_final_round_as_rounds_played():
    """3 players: the bye player (already in the final) disconnects; the round-1 winner wins the final by walkover."""
    game, events = await _bracket_game(num_players=3)
    first = game.current_match
    bye_player = await _state_bye(game)

    await game._process_gameplay_update(_frame(disconnects=[bye_player]))
    finished = False
    for frame in range(50):
        update = _frame([first.player1_serial], spike=[first.player1_serial]) if frame == 0 else _frame()
        if await game._process_gameplay_update(update):
            finished = True
            break

    assert finished
    announced = events.get_events_of_type("tournament_champion")
    assert [(e["champion"], e["rounds_played"]) for e in announced] == [(first.player2_serial, 2)]


@pytest.mark.uc("UC-025", br=["BR-006"])
def test_revive_is_denied_by_the_capability_matrix():
    assert "revive_player" in MODE_CAPABILITY_DENY["Tournament"]
    assert "eliminate_player" not in MODE_CAPABILITY_DENY["Tournament"]


@pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-001"])
@pytest.mark.uc("UC-030", flow="A1")
@pytest.mark.asyncio
async def test_an_agent_shield_protects_a_fighter_after_the_match_invincibility_ends():
    """BR-001: grace is max(agent shield, match invincibility): a shielded fighter cannot lose, then can."""
    game, _ = await _bracket_game()  # invincibility 0: only the shield protects
    match = game.current_match
    shielded = match.player1_serial
    game.players[shielded].grace_until = game_clock.now() + 60.0

    with patch("services.game_coordinator.games.base.metrics"):
        await game._process_gameplay_update(_frame([shielded], spike=[shielded]))
    assert game.current_match is match and not match.is_complete, "a shielded fighter lost the match"
    assert game.players[shielded].alive and game.dead_count == 0

    game.players[shielded].grace_until = game_clock.now() - 1.0  # the shield ends
    with patch("services.game_coordinator.games.base.metrics"):
        await game._process_gameplay_update(_frame([shielded], spike=[shielded]))
    assert match.is_complete and match.winner_serial == match.player2_serial


def _gameplay_data(serial, magnitude):
    return controller_manager_pb2.GameplayData(
        serial=serial, accel=controller_manager_pb2.Vector3(x=magnitude, y=0, z=0)
    )


@pytest.mark.uc("UC-030", flow="main", steps=[4])
@pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-002"])
@pytest.mark.asyncio
async def test_a_fighter_ema_uses_perception_ema_weight():
    """BR-002: Tournament's two fighters are smoothed with the configured EMA weight, like every mode."""
    game, _ = await _bracket_game()
    fighter = game.players[game.current_match.player1_serial]
    game._ema_weight = 1.0

    with patch.object(game, "_kill_player", autospec=True), patch.object(game, "_warn_player", autospec=True):
        await game._process_controller_state(_gameplay_data(fighter.serial, 1.0))
        await game._process_controller_state(_gameplay_data(fighter.serial, 2.0))

    # weight 1: (1.0 * 1 + 2.0) / 2 = 1.5 (a hard-coded weight 4 gives 1.2)
    assert fighter.smoothed_accel == pytest.approx(1.5)


@pytest.mark.uc("UC-030", flow="main", steps=[6, 7])
@pytest.mark.uc("UC-025", flow="main", steps=[3], br=["BR-002"])
@pytest.mark.asyncio
async def test_a_fighter_gets_at_most_one_warning_per_warning_window():
    """BR-002: between the warning and the death threshold a fighter is warned once, not killed."""
    game, _ = await _bracket_game()
    fighter = game.players[game.current_match.player1_serial]
    warn_threshold, death_threshold = game._compute_effective_thresholds(fighter)
    between = (warn_threshold + death_threshold) / 2

    with patch.object(game, "_kill_player", autospec=True) as kill:
        for _ in range(10):
            await game._process_controller_state(_gameplay_data(fighter.serial, between))

    kill.assert_not_called()
    warnings = [
        m
        for m in game.gameplay_stream.messages
        if m.HasField("game_effect")
        and m.game_effect.serial == fighter.serial
        and m.game_effect.effect == controller_manager_pb2.GAME_EFFECT_PLAYER_WARNING
    ]
    assert len(warnings) == 1


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-005"])
@pytest.mark.parametrize("failing_event", ["match_end", "tournament_champion"])
@pytest.mark.asyncio
async def test_an_error_after_the_champion_latch_keeps_the_completed_result(failing_event):
    """D-G34: a publish that fails while the decided final is finished is logged; no game_error."""
    game, log, client, _ = _game(2)
    real_publish = game.event_publisher

    async def publish(event_type, data):
        if event_type == failing_event:
            raise RuntimeError(f"{failing_event} publish failed")
        return await real_publish(event_type, data)

    game.event_publisher = publish

    await _run(game, client)  # a game_error would raise here

    assert game._champion is not None
    ended = _ended(log)
    assert ended["champion"] == game._champion
    assert ended["outcome"] == "completed"
    assert game.game_outcome() == "completed"
    assert GameEvent.GAME_ERROR.value not in [et for et, _ in log.events.events]
    if failing_event == "match_end":
        # The champion is still announced after the final's match_end failed.
        assert [e["champion"] for e in _events(log, "tournament_champion")] == [game._champion]


class _WriteFailsOnFinalDeathStream(_LethalStream):
    """The gameplay call fails under the write of the final loser's death effect; every later write fails too."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed = False

    async def write(self, message):
        match = self.game.current_match
        if (
            not self.failed
            and message.HasField("game_effect")
            and message.game_effect.effect == DEATH
            and match is not None
            and match.is_complete
            and self.game._is_final(match)
        ):
            self.failed = True
        if self.failed:
            self.rejected.append(message)
            raise grpc.aio.AioRpcError(grpc.StatusCode.UNAVAILABLE, None, None, "stream broke")
        await super().write(message)


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-005"])
@pytest.mark.uc("UC-030", br=["BR-006"])
@pytest.mark.asyncio
async def test_a_stream_error_in_the_frame_that_decided_the_final_keeps_the_result_without_a_reconnect():
    """D-G34: the final loser's death-effect write fails with a gRPC error: no reconnect, no game_error."""
    game, log, client, _ = _game(2)

    def open_stream():
        stream = _WriteFailsOnFinalDeathStream(log, game)
        client.streams.append(stream)
        return stream

    client.StreamGameplayData = open_stream

    await _run(game, client)  # a game_error would raise here

    stream = client.streams[0]
    assert stream.failed, "the final loser's death effect was never written"
    assert len(client.streams) == 1, "the failed call was reopened after the champion was latched"
    assert game._champion is not None
    assert [e["champion"] for e in _events(log, "tournament_champion")] == [game._champion]
    ended = _ended(log)
    assert ended["champion"] == game._champion
    assert ended["outcome"] == "completed"
    assert GameEvent.GAME_ERROR.value not in [et for et, _ in log.events.events]


@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-005"])
@pytest.mark.asyncio
async def test_an_error_in_the_deciding_kill_after_the_latch_keeps_the_completed_result():
    """D-G34: the kill path fails after the mode's handler latched the champion (the aggregate update)."""
    game, log, client, _ = _game(2)
    real_aggregate = game._set_players_alive_aggregate
    failures = []

    def aggregate(alive_count):
        if game._result_latched and not failures:
            failures.append(alive_count)
            raise RuntimeError("players_alive aggregate failed")
        real_aggregate(alive_count)

    game._set_players_alive_aggregate = aggregate

    await _run(game, client)  # a game_error would raise here

    assert failures, "the aggregate update after the latch never ran"
    assert game._champion is not None
    ended = _ended(log)
    assert ended["champion"] == game._champion
    assert ended["outcome"] == "completed"
    assert GameEvent.GAME_ERROR.value not in [et for et, _ in log.events.events]


@pytest.mark.uc("UC-025", flow="A1", steps=[2])
@pytest.mark.asyncio
async def test_the_timeout_loser_is_counted_out_without_a_death():
    """M3: the A1 loser gets the elimination gauges and the aggregate, but no death effect or explosion."""
    game, events = await _bracket_game()
    match = game.current_match
    spans = {serial: MagicMock() for serial in game.players}
    for serial, span in spans.items():
        game.players[serial].span = span
    game._match_deadline = time.monotonic() - 1.0

    with (
        patch("services.game_coordinator.games.base.metrics") as m,
        patch.object(game, "_set_players_alive_aggregate") as aggregate,
    ):
        await game._process_gameplay_update(_frame())

    winner = match.winner_serial
    loser = match.player2_serial if winner == match.player1_serial else match.player1_serial
    assert [(e["winner"], e["loser"]) for e in events.get_events_of_type("match_end")] == [(winner, loser)]
    assert game.players[loser].alive is False
    assert game.players[loser].tournament_state == TournamentState.ELIMINATED
    assert game.dead_count == 1 and game._elimination_count == 1
    assert _gauge_zero_serials(m) == [loser]
    m.player_alive.labels.return_value.set.assert_called_once_with(0)
    m.player_elimination_order.labels.assert_called_once_with(serial=loser, game_id=game.game_id)
    m.player_elimination_order.labels.return_value.set.assert_called_once_with(1)
    aggregate.assert_called_with(len(game.players) - 1)
    m.player_deaths_total.labels.assert_not_called()
    assert _explosions(game) == 0
    assert _death_effects(game) == []
    assert "player_death" not in [c.args[0] for c in spans[loser].add_event.call_args_list]
