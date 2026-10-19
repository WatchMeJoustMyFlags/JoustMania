"""games_completed_total{outcome} (#1427, maintainer decision 3).

The session labels each completed game with the game's own ``game.outcome``
(UC-020 BR-003, #1328): winner, tie, completed or force_ended, or with
``error`` when the session published game_error (no gRPC clients, mode creation
failed, game.run() raised). Nothing else reaches the label, so it adds at most
five series per (mode, game_kind).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService
from test_game_session import _make_session, _tracer_mock

from services.game_coordinator.game_session import GAME_KIND_PRIMARY, GAME_OUTCOMES, completed_game_outcome
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame


def _ffa_game(num_players: int, dead: int = 0) -> FFAGame:
    """A real FFA game with ``num_players`` players, the first ``dead`` already out."""
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=num_players),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="test_games_completed_outcome",
    )
    for i in range(num_players):
        game.players[f"p{i}"] = Player(serial=f"p{i}", alive=i >= dead, color=(255, 0, 0))
    return game


def _winner_game():
    game = _ffa_game(3, dead=2)
    game._latch_game_outcome()
    return game


def _tie_game():
    game = _ffa_game(2, dead=2)
    game._latch_game_outcome()
    return game


def _completed_game():
    # The loop stopped with several players alive and no force-end (ffa.py).
    game = _ffa_game(3, dead=0)
    game._latch_game_outcome()
    return game


def _force_ended_game():
    game = _ffa_game(4, dead=2)
    game.force_end("dry_run_2")
    return game


async def _completed_labels(game, mock_tracer_mod, mock_factory, mock_metrics):
    """Run a session whose factory returns ``game``; return the games_completed_total labels."""
    session = _make_session(game_kind=GAME_KIND_PRIMARY)
    mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
    game.run = AsyncMock()
    mock_factory.create_game.return_value = game

    await session._run_game_loop_async()

    mock_metrics.games_completed_total.labels.return_value.inc.assert_called_once()
    return mock_metrics.games_completed_total.labels.call_args.kwargs


class TestGamesCompletedOutcomeLabel:
    @pytest.mark.uc("UC-020", flow="main", br=["BR-003"])
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("build_game", "outcome"),
        [
            (_winner_game, "winner"),
            (_tie_game, "tie"),
            (_completed_game, "completed"),
            (_force_ended_game, "force_ended"),
        ],
    )
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_outcome_label_is_the_game_outcome(
        self, mock_tracer_mod, mock_factory, mock_metrics, build_game, outcome
    ):
        """Each UC-020 BR-003 outcome (and the default ``completed``) labels the counter."""
        game = build_game()
        assert game.game_outcome() == outcome

        labels = await _completed_labels(game, mock_tracer_mod, mock_factory, mock_metrics)

        assert labels == {"mode": "FFA", "game_kind": GAME_KIND_PRIMARY, "outcome": outcome}

    @pytest.mark.uc("UC-020", br=["BR-003"])
    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_win_then_late_force_end_stays_winner(self, mock_tracer_mod, mock_factory, mock_metrics):
        """A force-end after the latch (winner rainbow, preemption) does not relabel a win."""
        game = _winner_game()
        game.force_end("preempted_by_real_game")

        labels = await _completed_labels(game, mock_tracer_mod, mock_factory, mock_metrics)

        assert labels["outcome"] == "winner"

    @pytest.mark.uc("UC-020", br=["BR-003"])
    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_clients_not_connected_counts_error(self, mock_tracer_mod, mock_factory, mock_metrics):
        """No gRPC clients: the session publishes game_error and never builds a game: ``error``."""
        session = _make_session()
        session.clients.is_connected = False
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span

        await session._run_game_loop_async()

        mock_factory.create_game.assert_not_called()
        assert mock_metrics.games_completed_total.labels.call_args.kwargs["outcome"] == "error"

    @pytest.mark.uc("UC-020", br=["BR-003"])
    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_mode_creation_failure_counts_error(self, mock_tracer_mod, mock_factory, mock_metrics):
        """GameFactory raised ValueError (unknown mode): game_error, ``error``."""
        session = _make_session()
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_factory.create_game.side_effect = ValueError("Unknown game mode: 'BadGame'")

        await session._run_game_loop_async()

        assert mock_metrics.games_completed_total.labels.call_args.kwargs["outcome"] == "error"

    @pytest.mark.uc("UC-020", br=["BR-003"])
    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_game_run_raising_counts_error(self, mock_tracer_mod, mock_factory, mock_metrics):
        """game.run() raised: game_error, ``error``, not the game's (natural) outcome."""
        game = _completed_game()
        session = _make_session()
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        game.run = AsyncMock(side_effect=RuntimeError("loop crashed"))
        mock_factory.create_game.return_value = game

        await session._run_game_loop_async()

        error_calls = [c for c in session.event_bus.publish.call_args_list if c.args[0] == "game_error"]
        assert error_calls
        assert mock_metrics.games_completed_total.labels.call_args.kwargs["outcome"] == "error"

    @pytest.mark.uc("UC-020", flow="A4", br=["BR-003"])
    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.await_flag_providers_ready")
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_force_end_while_waiting_for_flags_counts_force_ended(
        self, mock_tracer_mod, mock_factory, mock_metrics, mock_await_ready
    ):
        """Force-ended before the game was built (flag-provider wait, #1329): ``force_ended``."""
        session = _make_session()
        session.game_running = True
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span

        async def _force_end_during_wait(_span):
            session.game_running = False

        mock_await_ready.side_effect = _force_end_during_wait

        await session._run_game_loop_async()

        mock_factory.create_game.assert_not_called()
        assert mock_metrics.games_completed_total.labels.call_args.kwargs["outcome"] == "force_ended"


class TestCompletedGameOutcomeIsBounded:
    @pytest.mark.uc("UC-020", br=["BR-003"])
    def test_only_the_five_outcomes(self):
        expected = {"winner", "tie", "completed", "force_ended", "error"}
        assert expected == GAME_OUTCOMES

    @pytest.mark.uc("UC-020", br=["BR-003"])
    def test_unknown_value_counts_completed(self):
        game = MagicMock(spec=FFAGame)
        game.game_outcome.return_value = "aborted"
        assert completed_game_outcome(game) == "completed"

    @pytest.mark.uc("UC-020", br=["BR-003"])
    def test_outcome_read_error_counts_completed(self):
        game = MagicMock(spec=FFAGame)
        game.game_outcome.side_effect = RuntimeError("boom")
        assert completed_game_outcome(game) == "completed"

    @pytest.mark.uc("UC-020", br=["BR-003"])
    def test_no_game_counts_completed(self):
        assert completed_game_outcome(None) == "completed"
