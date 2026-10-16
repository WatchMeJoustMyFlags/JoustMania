"""End-of-game outcome for JoustFFA: winner, tie, force_ended (UC-020 BR-003, #1328).

Dry run #2 game ``game_1c65c87d4f26`` (trace c8e9aa27347f60d5a5ac1649e8dd4233) was
force-ended at ~114 s by the dry-run driver (``ForceEndGame``, reason ``dry_run_2``)
with four players alive. Every survivor got a ``victory`` span event (``winner`` =
false), which read as four victories. A force-ended game now records outcome
``force_ended`` with the reason, and survivors get ``player_survived``.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from opentelemetry import trace

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import GameEvent
from proto import controller_manager_pb2
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame


class _RecordingStream:
    """Gameplay stream stand-in that records the effects written to it."""

    def __init__(self):
        self.writes = []

    async def write(self, message):
        self.writes.append(message)

    def effects(self):
        return [m.game_effect.effect for m in self.writes if m.HasField("game_effect")]


def _autospec_span():
    span = create_autospec(trace.Span, instance=True)
    span.get_span_context.return_value = trace.INVALID_SPAN_CONTEXT
    return span


async def _no_rainbow_wait():
    return True


def _ffa_game(num_players: int, dead: int = 0):
    """FFA game with ``num_players`` players (the first ``dead`` already out) and autospec'd spans."""
    events = EventCollector()
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=num_players),
        event_publisher=events.publish,
        audio_client=None,
        game_id="test_game_end_outcome",
    )
    game.gameplay_stream = _RecordingStream()
    game._wait_for_rainbow_effect = _no_rainbow_wait
    game.start_time = 1.0
    game.running = True
    for i in range(num_players):
        serial = f"p{i}"
        game.players[serial] = Player(serial=serial, alive=i >= dead, color=(255, 0, 0))
        game.players[serial].span = _autospec_span()
    return game, events


def _span_events(span, name):
    return [c.kwargs["attributes"] for c in span.add_event.call_args_list if c.args and c.args[0] == name]


def _end_events_by_player(spans):
    """{serial: [(event_name, attrs)]} of the end events on each player span."""
    out = {}
    for serial, span in spans.items():
        out[serial] = [(name, attrs) for name in ("victory", "player_survived") for attrs in _span_events(span, name)]
    return out


async def _finish(game):
    """Run the FFA end sequence the base run() runs: close spans, end, record outcome."""
    spans = {s: p.span for s, p in game.players.items()}
    root, teardown = _autospec_span(), _autospec_span()
    game._game_root_span = root
    game._close_all_player_spans()
    await game._end_game_impl()
    game._record_game_outcome(teardown)
    return spans, root, teardown


def _root_attrs(root):
    attrs = {}
    for c in root.set_attributes.call_args_list:
        attrs.update(c.args[0])
    return attrs


class TestWinnerOutcome:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[6, 7], br=["BR-001", "BR-003"])
    async def test_one_left_is_winner(self):
        """UC-020 BR-003 winner: victory only on the winner, outcome=winner with game.winner."""
        game, events = _ffa_game(3, dead=2)
        spans, root, teardown = await _finish(game)

        ends = _end_events_by_player(spans)
        assert [n for n, _ in ends["p2"]] == ["victory"]
        assert ends["p2"][0][1]["winner"] is True
        assert ends["p0"] == [] and ends["p1"] == []

        assert _root_attrs(root) == {"game.outcome": "winner", "game.winner": "p2"}
        assert _span_events(teardown, "game.outcome") == [{"game.outcome": "winner", "game.winner": "p2"}]
        (ended,) = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert ended["outcome"] == "winner"
        assert "reason" not in ended
        assert controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW in game.gameplay_stream.effects()


class TestTieOutcome:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A1", br=["BR-003"])
    async def test_none_left_is_tie(self):
        """UC-020 A1 / BR-003 tie: no victory, outcome=tie, no rainbow."""
        game, events = _ffa_game(3, dead=3)
        spans, root, teardown = await _finish(game)

        assert all(v == [] for v in _end_events_by_player(spans).values())
        assert _root_attrs(root) == {"game.outcome": "tie"}
        assert _span_events(teardown, "game.outcome") == [{"game.outcome": "tie"}]
        (ended,) = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert ended["outcome"] == "tie"
        assert controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW not in game.gameplay_stream.effects()


class TestForceEndedOutcome:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A4", br=["BR-003"])
    async def test_force_end_with_survivors_is_not_victories(self):
        """UC-020 A4 / BR-003: the dry-run case: 6 players, 2 dead, force-ended -> no victory."""
        game, events = _ffa_game(6, dead=2)
        game.force_end("dry_run_2")
        spans, root, teardown = await _finish(game)

        ends = _end_events_by_player(spans)
        survivors = {s: e for s, e in ends.items() if e}
        assert set(survivors) == {"p2", "p3", "p4", "p5"}
        for serial, evs in survivors.items():
            assert [n for n, _ in evs] == ["player_survived"], serial
            attrs = evs[0][1]
            assert attrs["winner"] is False
            assert attrs["game.outcome"] == "force_ended"
            assert attrs["game.end_reason"] == "dry_run_2"

        expected = {"game.outcome": "force_ended", "game.end_reason": "dry_run_2"}
        assert _root_attrs(root) == expected
        assert _span_events(teardown, "game.outcome") == [expected]
        (ended,) = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert (ended["outcome"], ended["reason"]) == ("force_ended", "dry_run_2")
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == []
        assert controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW not in game.gameplay_stream.effects()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A4", br=["BR-003"])
    async def test_force_end_with_one_left_still_force_ended(self):
        """UC-020 BR-003: force_ended takes precedence over a lone survivor: no winner, no rainbow."""
        game, events = _ffa_game(3, dead=2)
        game.force_end("operator_stop")
        spans, root, _ = await _finish(game)

        assert [n for n, _ in _end_events_by_player(spans)["p2"]] == ["player_survived"]
        assert _root_attrs(root)["game.outcome"] == "force_ended"
        assert "game.winner" not in _root_attrs(root)
        assert controller_manager_pb2.GAME_EFFECT_WINNER_RAINBOW not in game.gameplay_stream.effects()

    @pytest.mark.uc("UC-020", flow="A4", br=["BR-003"])
    def test_force_end_without_reason_is_unspecified(self):
        """UC-020 BR-003: a force-end with no reason still records force_ended (reason 'unspecified')."""
        game, _ = _ffa_game(2)
        game.force_end()
        assert game.running is False
        assert game._game_outcome_attributes() == {
            "game.outcome": "force_ended",
            "game.end_reason": "unspecified",
        }

    @pytest.mark.uc("UC-020", flow="main", br=["BR-003"])
    def test_not_force_ended_by_default(self):
        """UC-020 BR-003: a game that was never force-ended is not force_ended."""
        game, _ = _ffa_game(2)
        assert game.force_ended is False
        assert game.force_end_reason is None


class TestRunRecordsOutcome:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="A4", br=["BR-003"])
    async def test_run_force_ended_mid_game(self):
        """UC-020 A4: a real run() force-ended mid-game publishes game_ended outcome=force_ended."""
        events = EventCollector()
        game = FFAGame(
            controller_manager_client=MockControllerManagerService(num_controllers=4, infinite=True),
            event_publisher=events.publish,
            audio_client=None,
            game_id="test_run_force_end",
            initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(4)],
        )
        task = asyncio.create_task(game.run())
        for _ in range(200):  # up to 10 s for the game to reach RUNNING
            if events.get_events_of_type(GameEvent.GAME_STARTED):
                break
            await asyncio.sleep(0.05)
        assert events.get_events_of_type(GameEvent.GAME_STARTED), "game never started"

        game.force_end("dry_run_2")
        await asyncio.wait_for(task, timeout=10)

        (ended,) = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert (ended["outcome"], ended["reason"]) == ("force_ended", "dry_run_2")
        assert events.get_events_of_type(GameEvent.GAME_WINNER) == []
        assert not any(a["winner"] for a in events.get_events_of_type(GameEvent.PLAYER_ANALYTICS))


class TestWinLatchedBeforeForceEnd:
    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[6, 7], br=["BR-003"])
    async def test_force_end_during_winner_rainbow_keeps_the_win(self):
        """UC-020 BR-003: game_winner, victory, THEN force_end during the rainbow -> outcome stays winner."""
        game, events = _ffa_game(3, dead=2)
        # The loop met the win condition: game_winner is published and the outcome latched.
        assert await game._check_win_condition() is True
        game._latch_game_outcome()
        spans = {s: p.span for s, p in game.players.items()}
        root, teardown = _autospec_span(), _autospec_span()
        game._game_root_span = root
        game._close_all_player_spans()

        async def _rainbow_interrupted_by_preemption():
            game.force_end("preempted_by_real_game")
            return False

        game._wait_for_rainbow_effect = _rainbow_interrupted_by_preemption
        await game._end_game_impl()
        game._record_game_outcome(teardown)

        assert events.get_events_of_type(GameEvent.GAME_WINNER) == [{"serial": "p2"}]
        assert [n for n, _ in _end_events_by_player(spans)["p2"]] == ["victory"]
        (ended,) = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert ended["outcome"] == "winner"
        assert "reason" not in ended
        assert _root_attrs(root) == {"game.outcome": "winner", "game.winner": "p2"}

    @pytest.mark.uc("UC-020", flow="A4", br=["BR-003"])
    def test_force_end_before_the_loop_ended_is_force_ended(self):
        """UC-020 BR-003: without a latched outcome, a force-end still wins over a lone survivor."""
        game, _ = _ffa_game(3, dead=2)
        game.force_end("operator_stop")
        game._latch_game_outcome()
        assert game._game_outcome_attributes()["game.outcome"] == "force_ended"

    @pytest.mark.uc("UC-020", flow="A4", br=["BR-003"])
    def test_outcome_is_recorded_once(self):
        """UC-020 BR-003: the outcome lands on the root span once, even if recorded again."""
        game, _ = _ffa_game(2)
        root = _autospec_span()
        game._game_root_span = root
        game._record_game_outcome(_autospec_span())
        game.force_end("late")
        game._record_game_outcome(_autospec_span())
        game._record_force_ended_outcome_if_unrecorded()
        assert root.set_attributes.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-020", flow="main", steps=[6, 7], br=["BR-001", "BR-003"])
    async def test_run_win_then_force_end_keeps_winner(self):
        """UC-020 BR-003 (real run()): win condition met, then a preempting force_end -> game_ended winner."""
        events = EventCollector()
        game = FFAGame(
            controller_manager_client=MockControllerManagerService(
                num_controllers=3, death_schedule={0.3: 1, 0.6: 2}, infinite=True
            ),
            event_publisher=events.publish,
            audio_client=None,
            game_id="test_run_win_then_force_end",
            initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(3)],
        )
        task = asyncio.create_task(game.run())
        for _ in range(300):  # up to 15 s for the win
            if events.get_events_of_type(GameEvent.GAME_WINNER):
                break
            await asyncio.sleep(0.05)
        assert events.get_events_of_type(GameEvent.GAME_WINNER), "no winner"

        game.force_end("preempted_by_real_game")  # lands in the post-win hold / rainbow
        await asyncio.wait_for(task, timeout=15)

        (ended,) = events.get_events_of_type(GameEvent.GAME_ENDED)
        assert ended["outcome"] == "winner"
        assert "reason" not in ended
