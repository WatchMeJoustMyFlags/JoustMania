"""Fight Club disconnects, declined kills and interventions (UC-025 A4, BR-006; #1383).

A disconnect is a forfeit: the opponent wins the round and scores, the player
leaves the rotation for good and keeps its score. The kill gate takes a kill only
for the defender or the fighter of a round being fought, out of its grace, or for
a disconnected player still in the game; every other kill is declined before the
base death accounting, so an ``eliminate_player`` on it is refused.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import call, patch

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

from lib.types import Sound  # noqa: E402
from services.game_coordinator.games.fight_club import FightClubGame, FightState  # noqa: E402
from services.game_coordinator.intervention_errors import InterventionNotApplied  # noqa: E402
from services.game_coordinator.interventions import (  # noqa: E402
    BLOCK_ELIMINATE_REFUSED,
    INTERVENTION_SPECS,
    MODE_CAPABILITY_DENY,
    InterventionContext,
)
from services.game_coordinator.lifecycle_handlers import handle_eliminate_player  # noqa: E402

FC_METRICS = "services.game_coordinator.games.fight_club.metrics"
BASE_METRICS = "services.game_coordinator.games.base.metrics"


async def _round_on(num_players: int, game_id: str, *, invincible: bool = False, min_rounds: int = 10):
    """A running Fight Club game with its first round seated (defender vs fighter)."""
    game, mock_cm, events = _make_game(num_players, game_id)
    game._min_rounds = min_rounds
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = RecordingGameplayStream()
    game._pause_between_rounds = _no_pause
    game.running = True
    await game._start_round()
    if not invincible:
        _clear_invincibility(game)
    return game, events


def _eliminate_ctx(game: FightClubGame, serial: str) -> InterventionContext:
    return InterventionContext(
        spec=next(s for s in INTERVENTION_SPECS if s.flag_key == "eliminate_player"),
        value=f"n1:{serial}",
        payload=serial,
        target_serial=serial,
        game=game,
        objective="balanced",
    )


def _counted(game: FightClubGame) -> tuple[int, int]:
    """The base death accounting: dead_count and the elimination order."""
    return game.dead_count, game._elimination_count


class _GatedExplosion:
    """``_play_sound`` replacement that holds the death explosion until released."""

    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, sound, *args, **kwargs):  # noqa: ARG002
        if sound == Sound.SFX_EXPLOSION:
            self.started.set()
            await self.release.wait()


class _GatedEffectStream(RecordingGameplayStream):
    """Holds the first ``game_effect`` write (the round loser's death effect) until released."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def write(self, message):
        if message.HasField("game_effect") and not self.started.is_set():
            self.started.set()
            await self.release.wait()
        await super().write(message)


class TestFightClubDisconnectForfeit:
    """UC-025 A4 (Fight Club part): a disconnect is a forfeit, the player is out for good."""

    @pytest.mark.uc("UC-025", flow="A4", steps=[4], br=["BR-006"])
    @pytest.mark.asyncio
    async def test_defender_disconnect_during_invincibility_the_fighter_wins(self):
        game, events = await _round_on(3, "test_fc_dc_defender_invincible", invincible=True)
        defender, fighter = game.current_defender, game.current_fighter
        assert game.is_in_grace(game.players[defender])

        with patch(FC_METRICS):
            await game._handle_disconnect(defender)

        assert game.players[fighter].score == 1
        assert game.current_defender == fighter
        assert game.players[defender].alive is False
        assert defender not in game.queue
        assert defender != game.current_fighter
        round_end = events.get_events_of_type("round_end")
        assert round_end and round_end[0]["winner"] == fighter and round_end[0]["loser"] == defender
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[5])
    @pytest.mark.asyncio
    async def test_waiting_player_disconnect_never_fights_again(self):
        game, events = await _round_on(4, "test_fc_dc_waiting", min_rounds=100)
        waiting = game.queue[0]
        score_before = {s: p.score for s, p in game.players.items()}

        with patch(FC_METRICS):
            await game._handle_disconnect(waiting)
            assert waiting not in game.queue
            assert game.players[waiting].alive is False
            # No round was decided by the waiting player's disconnect.
            assert {s: p.score for s, p in game.players.items()} == score_before
            assert not events.get_events_of_type("round_end")
            # Play many rounds: the player who left is never seated again.
            for _ in range(8):
                _clear_invincibility(game)
                await game._kill_player(game.current_fighter, 5.0)
                assert waiting not in (game.current_defender, game.current_fighter)
        assert game.running and not game.game_over
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[5])
    @pytest.mark.asyncio
    async def test_loser_disconnect_during_the_pause_leaves_no_ghost(self):
        """An eliminate_player round loss pauses 1 s; the loser disconnects meanwhile (the base drops that kill)."""
        game, _ = await _round_on(3, "test_fc_dc_pause_loser", min_rounds=100)
        pause_started, pause_release = asyncio.Event(), asyncio.Event()

        async def held_pause():
            pause_started.set()
            await pause_release.wait()

        game._pause_between_rounds = held_pause
        loser = game.current_fighter

        with patch(FC_METRICS) as fc_metrics:
            kill = asyncio.create_task(game._kill_player(loser, 5.0))
            await asyncio.wait_for(pause_started.wait(), 1.0)
            assert loser in game.queue
            await game._handle_disconnect(loser)
            assert loser not in game.queue
            assert game.players[loser].alive is False
            fc_metrics.player_alive.labels.return_value.set.assert_called_with(0)
            pause_release.set()
            assert await kill is True

        assert loser not in (game.current_defender, game.current_fighter)
        assert loser not in game.queue
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[6])
    @pytest.mark.asyncio
    async def test_two_players_one_disconnects_the_game_ends(self):
        game, events = await _round_on(2, "test_fc_dc_two_players")
        leaver, stayer = game.current_fighter, game.current_defender

        with patch(FC_METRICS):
            await game._handle_disconnect(leaver)

        assert game.game_over is True
        assert await game._check_win_condition() is True
        assert game._latched_winner == stayer
        assert game.game_outcome() == "completed"
        assert not events.get_events_of_type("round_start")[1:]
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[6])
    @pytest.mark.asyncio
    async def test_a_disconnected_leader_can_still_be_crowned(self):
        game, _ = await _round_on(3, "test_fc_dc_leader_crowned", min_rounds=100)
        leader = game.queue[0]
        game.players[leader].score = 5

        with patch(FC_METRICS):
            await game._handle_disconnect(leader)
            assert game.game_over is False
            await game._handle_disconnect(game.current_fighter)

        assert game.game_over is True
        assert game._latched_winner == leader
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[6])
    @pytest.mark.asyncio
    async def test_tied_top_with_no_face_off_possible_has_no_winner(self):
        game, _ = await _round_on(2, "test_fc_dc_tied_no_winner")
        leaver = game.current_fighter
        defender = game.current_defender
        # The forfeit gives the defender a point; start the leaver one ahead so the top ties.
        game.players[leaver].score = 1

        with patch(FC_METRICS):
            await game._handle_disconnect(leaver)

        assert game.players[defender].score == 1
        assert game.game_over is True
        assert game._latched_winner is None
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[6], br=["BR-005"])
    @pytest.mark.asyncio
    async def test_a_disconnect_after_the_latch_is_ignored(self):
        game, _ = await _round_on(2, "test_fc_dc_after_latch", min_rounds=1)
        winner = game.current_defender

        with patch(FC_METRICS):
            await game._kill_player(game.current_fighter, 5.0)
            assert game._latched_winner == winner
            dead_before = _counted(game)
            await game._handle_disconnect(winner)

        assert winner not in game._disconnected
        assert game.players[winner].alive is True
        assert _counted(game) == dead_before
        assert game._latched_winner == winner
        _end_round_timer(game)


class TestFightClubKillGate:
    """UC-025 BR-006 (Fight Club part, #1383): declined kills count nothing; eliminate_player is refused."""

    @pytest.mark.uc("UC-025", flow="A2", steps=[2], br=["BR-006"])
    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_eliminate_between_rounds_is_refused_and_the_defender_stays_killable(self):
        game, _ = await _round_on(3, "test_fc_gate_between_rounds", min_rounds=100)
        pause_started, pause_release = asyncio.Event(), asyncio.Event()

        async def held_pause():
            pause_started.set()
            await pause_release.wait()

        game._pause_between_rounds = held_pause
        round_winner = game.current_defender

        with patch(FC_METRICS):
            kill = asyncio.create_task(game._kill_player(game.current_fighter, 5.0))
            await asyncio.wait_for(pause_started.wait(), 1.0)
            assert game.current_fighter is None
            counted = _counted(game)

            with pytest.raises(InterventionNotApplied) as refused:
                await handle_eliminate_player(_eliminate_ctx(game, round_winner))
            assert refused.value.reason == BLOCK_ELIMINATE_REFUSED
            assert _counted(game) == counted
            assert game.players[round_winner].alive is True

            pause_release.set()
            await kill
            # The next round is seated with the round winner as defender, and it can lose it.
            assert game.current_defender == round_winner
            _clear_invincibility(game)
            await handle_eliminate_player(_eliminate_ctx(game, round_winner))
        assert game.current_defender != round_winner
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A2", steps=[2], br=["BR-006"])
    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_eliminate_of_a_waiting_player_is_refused_and_counts_nothing(self):
        game, events = await _round_on(3, "test_fc_gate_waiting")
        waiting = game.queue[0]
        counted = _counted(game)
        sounds = []

        async def record_sound(sound, *args, **kwargs):  # noqa: ARG001
            sounds.append(sound)

        game._play_sound = record_sound
        with patch(FC_METRICS), pytest.raises(InterventionNotApplied) as refused:
            await handle_eliminate_player(_eliminate_ctx(game, waiting))

        assert refused.value.reason == BLOCK_ELIMINATE_REFUSED
        assert _counted(game) == counted
        assert Sound.SFX_EXPLOSION not in sounds
        assert game.players[waiting].alive is True
        assert game.queue[0] == waiting
        assert not events.get_events_of_type("round_end")
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A2", steps=[2], br=["BR-001", "BR-006"])
    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_eliminate_of_an_invincible_fighter_is_refused(self):
        game, _ = await _round_on(3, "test_fc_gate_invincible", invincible=True)
        fighter = game.current_fighter
        counted = _counted(game)

        with patch(FC_METRICS), pytest.raises(InterventionNotApplied):
            await handle_eliminate_player(_eliminate_ctx(game, fighter))

        assert _counted(game) == counted
        assert game.current_fighter == fighter
        assert all(p.score == 0 for p in game.players.values())
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A2", steps=[4], br=["BR-005", "BR-006"])
    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_eliminate_after_the_winner_is_latched_is_refused(self):
        game, _ = await _round_on(3, "test_fc_gate_after_latch", min_rounds=1)
        defender, fighter = game.current_defender, game.current_fighter

        with patch(FC_METRICS):
            await game._kill_player(fighter, 5.0)
        assert game.game_over is True
        counted = _counted(game)
        scores = {s: p.score for s, p in game.players.items()}

        with patch(FC_METRICS), pytest.raises(InterventionNotApplied) as refused:
            await handle_eliminate_player(_eliminate_ctx(game, defender))

        assert refused.value.reason == BLOCK_ELIMINATE_REFUSED
        assert _counted(game) == counted
        assert {s: p.score for s, p in game.players.items()} == scores
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", br=["BR-006"])
    def test_revive_is_denied_by_the_capability_matrix(self):
        assert "revive_player" in MODE_CAPABILITY_DENY["Fight Club"]
        assert "eliminate_player" not in MODE_CAPABILITY_DENY["Fight Club"]

    @pytest.mark.uc("UC-025", flow="A2", steps=[2], br=["BR-006"])
    @pytest.mark.uc("UC-040", flow="A22")
    @pytest.mark.asyncio
    async def test_eliminate_of_the_fighter_is_a_round_loss(self):
        game, events = await _round_on(3, "test_fc_gate_round_loss", min_rounds=100)
        defender, fighter = game.current_defender, game.current_fighter

        with patch(FC_METRICS):
            await handle_eliminate_player(_eliminate_ctx(game, fighter))

        assert game.players[defender].score == 1
        assert game.players[fighter].alive is True
        assert fighter in game.queue
        assert events.get_events_of_type("round_end")[0]["loser"] == fighter
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A2", steps=[2], br=["BR-006"])
    @pytest.mark.asyncio
    async def test_timeout_during_a_kill_in_flight_declines_the_kill(self):
        """Timer vs kill race with invincibility 0: the timeout decided the round while the kill awaited."""
        game, events = await _round_on(3, "test_fc_gate_timer_race", invincible=True, min_rounds=100)
        game._invincibility_duration = 0.0
        _clear_invincibility(game)
        explosion = _GatedExplosion()
        game._play_sound = explosion
        timeout_published, timeout_release = asyncio.Event(), asyncio.Event()
        publish = game.event_publisher

        async def held_timeout_publish(event_type, data):
            await publish(event_type, data)
            if event_type == "round_timeout":
                timeout_published.set()
                await timeout_release.wait()

        game.event_publisher = held_timeout_publish
        defender = game.current_defender
        counted = _counted(game)

        with patch(FC_METRICS):
            kill = asyncio.create_task(game._kill_player(defender, 5.0))
            await asyncio.wait_for(explosion.started.wait(), 1.0)
            timeout = asyncio.create_task(game._handle_timeout())
            await asyncio.wait_for(timeout_published.wait(), 1.0)
            explosion.release.set()
            assert await kill is False
            timeout_release.set()
            await timeout

        assert _counted(game) == counted
        assert all(p.score == 0 for p in game.players.values())
        assert game.players[defender].alive is True
        assert not events.get_events_of_type("round_end")
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[5], br=["BR-006"])
    @pytest.mark.asyncio
    async def test_a_disconnect_while_its_kill_is_in_flight_counts_one_death(self):
        """A fighter's kill awaits its explosion; the round times out and the fighter, now waiting, disconnects.

        The base drops the disconnect kill (one is in flight); the kill in flight then takes the
        player out of the rotation through the death path: exactly one death is counted.
        """
        game, _ = await _round_on(3, "test_fc_gate_one_death", min_rounds=100)
        explosion = _GatedExplosion()
        game._play_sound = explosion
        fighter = game.current_fighter
        dead_count, order = _counted(game)

        with patch(FC_METRICS) as fc_metrics, patch(BASE_METRICS) as base_metrics:
            kill = asyncio.create_task(game._kill_player(fighter, 5.0))
            await asyncio.wait_for(explosion.started.wait(), 1.0)
            await game._handle_timeout()
            assert fighter in game.queue
            await game._handle_disconnect(fighter)
            explosion.release.set()
            assert await kill is True

            assert base_metrics.player_deaths_total.labels.return_value.inc.call_count == 1
            assert call(1) not in fc_metrics.player_alive.labels.return_value.set.call_args_list

        assert _counted(game) == (dead_count + 1, order + 1)
        assert game.players[fighter].alive is False
        assert fighter not in game.queue
        assert fighter not in (game.current_defender, game.current_fighter)
        assert all(p.score == 0 for p in game.players.values())
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[4, 5, 6], br=["BR-006"])
    @pytest.mark.asyncio
    async def test_a_round_loser_seated_again_that_disconnects_forfeits_the_seated_round(self):
        """Two players: eliminate_player on the fighter; it disconnects while the next ``round_start`` is published.

        The next round has seated the loser again (nobody else is left to fight); its round-loss kill
        is still in flight, so the disconnect waits for it and then forfeits the seated round.
        """
        game, events = await _round_on(2, "test_fc_dc_seated_loser")
        defender, loser = game.current_defender, game.current_fighter
        round_start_published, round_start_release = asyncio.Event(), asyncio.Event()
        publish = game.event_publisher

        async def held_round_start(event_type, data):
            await publish(event_type, data)
            if event_type == "round_start":
                round_start_published.set()
                await round_start_release.wait()

        game.event_publisher = held_round_start
        dead_count, order = _counted(game)

        with patch(FC_METRICS):
            kill = asyncio.create_task(handle_eliminate_player(_eliminate_ctx(game, loser)))
            await asyncio.wait_for(round_start_published.wait(), 1.0)
            assert game.current_fighter == loser
            await game._handle_disconnect(loser)
            round_start_release.set()
            await kill

        assert game.players[loser].alive is False
        assert loser not in (game.current_defender, game.current_fighter)
        assert game.players[defender].score == 2
        assert [e["loser"] for e in events.get_events_of_type("round_end")] == [loser, loser]
        # Two deaths, one elimination: the round loss is not one, leaving the rotation is (UC-041 BR-008, #2517).
        assert _counted(game) == (dead_count + 2, order + 1)
        assert game.game_over is True
        assert game._latched_winner == defender
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", br=["BR-005"])
    @pytest.mark.asyncio
    async def test_loser_gauge_is_not_reset_once_the_game_is_decided(self):
        """#2002 guard: the latch lands while the loser's death effect is written (the winner leaves)."""
        game, _ = await _round_on(2, "test_fc_gauge_after_latch")
        stream = _GatedEffectStream()
        game.gameplay_stream = stream
        winner, loser = game.current_defender, game.current_fighter

        with patch(FC_METRICS) as fc_metrics:
            kill = asyncio.create_task(game._kill_player(loser, 5.0))
            await asyncio.wait_for(stream.started.wait(), 1.0)
            await game._handle_disconnect(winner)
            assert game._win_latched is True
            stream.release.set()
            await kill

            assert game.players[loser].alive is True
            assert game._latched_winner == winner
            assert call(1) not in fc_metrics.player_alive.labels.return_value.set.call_args_list
        _end_round_timer(game)

    @pytest.mark.uc("UC-025", flow="A4", steps=[4, 5])
    @pytest.mark.asyncio
    async def test_start_round_skips_a_disconnected_serial(self):
        game, _ = await _round_on(4, "test_fc_start_round_skips", min_rounds=100)
        ghost = game.queue[0]
        game._disconnected.add(ghost)

        with patch(FC_METRICS):
            await game._kill_player(game.current_fighter, 5.0)

        assert ghost not in (game.current_defender, game.current_fighter)
        assert ghost not in game.queue
        assert game.players[ghost].state == FightState.IN_LINE
        _end_round_timer(game)
