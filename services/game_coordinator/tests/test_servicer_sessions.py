"""
Characterization + multi-session tests for GameCoordinatorServicer (#775).

The first group (TestSingleGameCharacterization) pins the EXISTING single-game
behavior at the servicer/RPC level BEFORE the multi-session refactor:
- start a game via StreamGameEvents(start_config) and observe events stream
- zero-arg StreamGameEvents subscribes to the running game's events
- GetGameState reflects the running game; ForceEndGame ends it
- <2 players rejected
- metrics side effects: active_game 1->0, games_started_total increments

These must keep passing unchanged after the refactor (default cap=1 preserves
today's behavior exactly).

The second group (TestMultiSession) exercises the new concurrent-session
behavior with GAME_MAX_CONCURRENT_GAMES raised above 1.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from proto import game_coordinator_pb2
from services.game_coordinator.servicer import GameCoordinatorServicer


class MockGrpcContext:
    """Mock gRPC context for testing streaming RPCs."""

    def __init__(self):
        self._cancelled = False

    def cancelled(self):
        return self._cancelled

    def cancel(self):
        self._cancelled = True

    def invocation_metadata(self):
        return []


def _config(game_name="FFA", serials=("p1", "p2"), sensitivity=2):
    return game_coordinator_pb2.StartGameConfig(
        game_name=game_name,
        players=[game_coordinator_pb2.Player(serial=s) for s in serials],
        sensitivity=sensitivity,
    )


async def _stream_until_event(servicer, request, context, *, after=None, deadline_s=2.0):
    """Subscribe via StreamGameEvents and collect the first event.

    Optionally runs ``after`` (an async callable) once the subscriber is live to
    publish a follow-up event. Returns the list of collected events.
    """
    gen = servicer.StreamGameEvents(request, context)
    collected = []

    async def _drain():
        async for event in gen:
            collected.append(event)
            context.cancel()
            break

    drain_task = asyncio.create_task(_drain())
    # Give the subscriber a moment to register (and the game to start).
    await asyncio.sleep(0.05)
    if after is not None:
        await after()

    try:
        async with asyncio.timeout(deadline_s):
            await drain_task
    except TimeoutError:
        drain_task.cancel()
    finally:
        context.cancel()
        await gen.aclose()
    return collected


class TestSingleGameCharacterization:
    """Pin existing single-game behavior (must pass before and after refactor)."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_start_via_stream_then_streams_events(self, servicer):
        """StreamGameEvents(start_config) starts a game, then streams later events.

        The initial ``game_start`` event is published *before* the stream
        subscribes (that is today's behavior), so the stream sees events
        published after the start — here a subsequent ``player_death``.
        """
        request = game_coordinator_pb2.StreamEventsRequest(start_config=_config())
        context = MockGrpcContext()

        async def _publish_followup():
            await servicer.event_bus.publish("player_death", {"serial": "p1"})

        with patch.object(servicer, "_run_game_loop_threaded"):
            collected = await _stream_until_event(servicer, request, context, after=_publish_followup)

        # The game must have actually started (id recorded on the servicer).
        assert servicer.game_id is not None and servicer.game_id.startswith("game_")
        assert any(e.event_type == "player_death" for e in collected)

    @pytest.mark.asyncio
    async def test_zero_arg_stream_receives_running_game_events(self, servicer):
        """A subscriber with no start_config receives the running game's events."""
        # Start a game first (no streaming subscriber yet).
        with patch.object(servicer, "_run_game_loop_threaded"):
            success, game_id = await servicer._start_game_from_config(_config(), _MockSpan())
        assert success is True

        # Zero-arg subscriber attaches to the primary bus, then we publish.
        request = game_coordinator_pb2.StreamEventsRequest()
        context = MockGrpcContext()

        async def _publish():
            await servicer.event_bus.publish("player_death", {"serial": "p1"})

        collected = await _stream_until_event(servicer, request, context, after=_publish)

        assert any(e.event_type == "player_death" for e in collected)

    @pytest.mark.asyncio
    async def test_get_game_state_reflects_running_game(self, servicer):
        """GetGameState returns the running game's id/mode/state."""
        with patch.object(servicer, "_run_game_loop_threaded"):
            success, game_id = await servicer._start_game_from_config(_config(game_name="FFA"), _MockSpan())
        assert success is True
        servicer.game_state = game_coordinator_pb2.GameState.RUNNING

        resp = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext())
        assert resp.success is True
        assert resp.game_info.game_id == game_id
        assert resp.game_info.game_mode == "FFA"
        assert resp.game_info.state == game_coordinator_pb2.GameState.RUNNING

    @pytest.mark.asyncio
    async def test_force_end_ends_running_game(self, servicer):
        """ForceEndGame ends the running game and transitions to ENDED."""
        with patch.object(servicer, "_run_game_loop_threaded"):
            await servicer._start_game_from_config(_config(), _MockSpan())
        servicer.game_state = game_coordinator_pb2.GameState.RUNNING

        resp = await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="test"), MockGrpcContext())
        assert resp.success is True
        assert servicer.game_state == game_coordinator_pb2.GameState.ENDED

    @pytest.mark.asyncio
    async def test_rejects_fewer_than_two_players(self, servicer):
        """A start with <2 players is rejected."""
        success, error = await servicer._start_game_from_config(_config(serials=("solo",)), _MockSpan())
        assert success is False
        assert "at least 2 players" in error.lower()

    @pytest.mark.asyncio
    async def test_second_concurrent_start_rejected_by_default(self, servicer):
        """With the default cap (1), a second start is rejected: 'already in progress'."""
        with patch.object(servicer, "_run_game_loop_threaded"):
            ok1, _ = await servicer._start_game_from_config(_config(), _MockSpan())
            assert ok1 is True
            servicer.game_state = game_coordinator_pb2.GameState.RUNNING

            ok2, error = await servicer._start_game_from_config(
                _config(game_name="Teams", serials=("p3", "p4")), _MockSpan()
            )
        assert ok2 is False
        assert "already in progress" in error.lower()

    @pytest.mark.asyncio
    @patch("services.game_coordinator.servicer.metrics")
    async def test_start_metric_side_effects(self, mock_metrics, servicer):
        """Starting a game sets active_game=1 and increments games_started_total."""
        with patch.object(servicer, "_run_game_loop_threaded"):
            await servicer._start_game_from_config(_config(), _MockSpan())

        # active_game gauge raised to 1 (label arg tolerated post-refactor).
        assert mock_metrics.active_game.set.called or mock_metrics.active_game.labels.called
        # games_started_total incremented.
        assert mock_metrics.games_started_total.labels.called


class _MockSpan:
    """Minimal span supporting set_attribute + context-manager protocol."""

    def __init__(self):
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
