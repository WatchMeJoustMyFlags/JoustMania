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
import os
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

from lib.types import GameEvent
from proto import game_coordinator_pb2
from services.game_coordinator import game_session as game_session_mod
from services.game_coordinator.servicer import GameCoordinatorServicer

# Patch target: stops the real game thread from spinning up while still
# registering the session and publishing game_start. Tests drive RUNNING via the
# session's EventBus (publish GAME_STARTED) rather than mutating state directly,
# so they are agnostic to the servicer's internal structure.
_NO_THREAD = patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None)


async def _start_running(servicer, config):
    """Start a game (no real thread) and drive it to RUNNING via its bus.

    Returns (game_id, session).
    """
    with _NO_THREAD:
        success, game_id = await servicer._start_game_from_config(config, _MockSpan())
    assert success is True, game_id
    session = servicer.sessions[game_id]
    # GAME_STARTED on the session bus transitions the session to RUNNING.
    await session.event_bus.publish(GameEvent.GAME_STARTED, {"game_id": game_id})
    return game_id, session


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
            # The just-started session is the primary; publish on the primary bus
            # the stream subscribed to.
            await servicer.event_bus.publish("player_death", {"serial": "p1"})

        with _NO_THREAD:
            collected = await _stream_until_event(servicer, request, context, after=_publish_followup)

        # The game must have actually started (id recorded on the servicer).
        assert servicer.game_id is not None and servicer.game_id.startswith("game_")
        assert any(e.event_type == "player_death" for e in collected)

    @pytest.mark.asyncio
    async def test_zero_arg_stream_receives_running_game_events(self, servicer):
        """A subscriber with no start_config receives the running game's events."""
        # Start a game first (no streaming subscriber yet).
        await _start_running(servicer, _config())

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
        game_id, _ = await _start_running(servicer, _config(game_name="FFA"))

        resp = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext())
        assert resp.success is True
        assert resp.game_info.game_id == game_id
        assert resp.game_info.game_mode == "FFA"
        assert resp.game_info.state == game_coordinator_pb2.GameState.RUNNING

    @pytest.mark.asyncio
    async def test_force_end_ends_running_game(self, servicer):
        """ForceEndGame ends the running game and transitions to ENDED."""
        await _start_running(servicer, _config())

        resp = await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="test"), MockGrpcContext())
        assert resp.success is True
        # After force-end the primary slot frees; no live primary -> IDLE.
        assert servicer.game_state == game_coordinator_pb2.GameState.IDLE

    @pytest.mark.asyncio
    async def test_rejects_fewer_than_two_players(self, servicer):
        """A start with <2 players is rejected."""
        success, error = await servicer._start_game_from_config(_config(serials=("solo",)), _MockSpan())
        assert success is False
        assert "at least 2 players" in error.lower()

    @pytest.mark.asyncio
    async def test_new_game_can_start_after_natural_end(self, servicer):
        """Regression (#775, integration CI): a naturally-ended game (win
        condition, no ForceEndGame) must not occupy the concurrency slot or pin
        the primary role — the next start must succeed at the default cap of 1.
        """
        gid1, session1 = await _start_running(servicer, _config(serials=("a1", "a2")))
        # Natural end: the game publishes its ending event; nobody force-ends.
        await session1.event_bus.publish(GameEvent.GAME_ENDED, {"game_id": gid1})
        assert session1.game_state == game_coordinator_pb2.GameState.ENDED

        # Legacy behavior: GetGameState keeps reporting the ended game until
        # the next start (lazy retirement happens at start time).
        state = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext())
        assert state.game_info.state == game_coordinator_pb2.GameState.ENDED

        gid2, session2 = await _start_running(servicer, _config(serials=("b1", "b2")))
        assert gid2 != gid1
        assert gid1 not in servicer.sessions
        assert session2.game_kind == "primary"
        assert servicer._primary_game_id == gid2
        # The new primary reuses the persistent primary bus (menu compat).
        assert session2.event_bus is servicer.primary_event_bus

    @pytest.mark.asyncio
    async def test_second_concurrent_start_rejected_by_default(self, servicer):
        """With the default cap (1), a second start is rejected: 'already in progress'."""
        with _NO_THREAD:
            ok1, _ = await servicer._start_game_from_config(_config(), _MockSpan())
            assert ok1 is True

            ok2, error = await servicer._start_game_from_config(
                _config(game_name="Teams", serials=("p3", "p4")), _MockSpan()
            )
        assert ok2 is False
        assert "already in progress" in error.lower()

    @pytest.mark.asyncio
    @patch("services.game_coordinator.servicer.metrics")
    async def test_start_metric_side_effects(self, mock_metrics, servicer):
        """Starting a game sets active_game=1 and increments games_started_total."""
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(), _MockSpan())

        # active_game gauge raised to 1 (now via the game_kind label).
        assert mock_metrics.active_game.labels.called
        mock_metrics.active_game.labels.return_value.set.assert_any_call(1)
        # games_started_total incremented.
        assert mock_metrics.games_started_total.labels.called


class TestMultiSession:
    """Concurrent-session behavior with GAME_MAX_CONCURRENT_GAMES raised (#775)."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.fixture
    def cap_two(self):
        """Raise the concurrent-games cap to 2 for the duration of a test."""
        with patch.dict(os.environ, {"GAME_MAX_CONCURRENT_GAMES": "2"}):
            yield

    @pytest.mark.asyncio
    async def test_two_sessions_distinct_ids_and_kinds(self, servicer, cap_two):
        """Two concurrent starts get distinct game_ids; first primary, second shadow."""
        with _NO_THREAD:
            ok1, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            ok2, gid2 = await servicer._start_game_from_config(_config(serials=("b1", "b2")), _MockSpan())

        assert ok1 and ok2
        assert gid1 != gid2
        assert servicer.sessions[gid1].game_kind == "primary"
        assert servicer.sessions[gid2].game_kind == "shadow"
        # Only the first session owns the primary slot.
        assert servicer.game_id == gid1

    @pytest.mark.asyncio
    async def test_two_sessions_independent_event_streams(self, servicer, cap_two):
        """Each session's bus only delivers its own game's events."""
        with _NO_THREAD:
            _, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            _, gid2 = await servicer._start_game_from_config(_config(serials=("b1", "b2")), _MockSpan())

        bus1 = servicer.sessions[gid1].event_bus
        bus2 = servicer.sessions[gid2].event_bus
        # Distinct EventBus instances (shadow gets its own; primary uses the
        # persistent bus).
        assert bus1 is not bus2
        assert bus1 is servicer.primary_event_bus

        q1 = await bus1.subscribe("sub1")
        q2 = await bus2.subscribe("sub2")

        await bus1.publish("player_death", {"serial": "a1", "game_id": gid1})
        await bus2.publish("player_death", {"serial": "b1", "game_id": gid2})

        e1 = q1.get_nowait()
        e2 = q2.get_nowait()
        assert e1.data["game_id"] == gid1
        assert e2.data["game_id"] == gid2
        # Neither queue saw the other's event.
        assert q1.empty()
        assert q2.empty()

    @pytest.mark.asyncio
    async def test_third_start_rejected_at_cap(self, servicer, cap_two):
        """With cap=2, the third concurrent start is rejected."""
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            await servicer._start_game_from_config(_config(serials=("b1", "b2")), _MockSpan())
            ok3, error = await servicer._start_game_from_config(_config(serials=("c1", "c2")), _MockSpan())

        assert ok3 is False
        assert "already in progress" in error.lower()

    @pytest.mark.asyncio
    async def test_force_end_targets_primary_only(self, servicer, cap_two):
        """ForceEndGame (no game_id) ends the primary and frees the primary slot."""
        with _NO_THREAD:
            _, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            _, gid2 = await servicer._start_game_from_config(_config(serials=("b1", "b2")), _MockSpan())

            resp = await servicer.ForceEndGame(
                game_coordinator_pb2.ForceEndGameRequest(reason="test"), MockGrpcContext()
            )

        assert resp.success is True
        # Primary retired; shadow still registered.
        assert gid1 not in servicer.sessions
        assert gid2 in servicer.sessions
        assert servicer._primary_game_id is None

    @pytest.mark.asyncio
    async def test_new_primary_after_primary_ends(self, servicer, cap_two):
        """Once the primary ends, a new session can claim the primary slot."""
        with _NO_THREAD:
            _, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="done"), MockGrpcContext())
            # New start with no live primary becomes primary again.
            _, gid2 = await servicer._start_game_from_config(_config(serials=("b1", "b2")), _MockSpan())

        assert servicer.sessions[gid2].game_kind == "primary"
        assert servicer._primary_game_id == gid2

    def test_shadow_end_clears_only_its_serials_and_keeps_global_gauges(self):
        """Cross-wipe fix: a shadow session's cleanup is targeted, not global.

        ``clear_session_player_analytics`` for a shadow session must remove ONLY
        the shadow's serials (via the targeted ``clear_player_analytics``) and
        must NOT reset the global single-game gauges (music_tempo etc.), so the
        live primary game's dashboard survives a shadow game ending.
        """
        from services.game_coordinator import metrics

        with (
            patch.object(metrics, "clear_player_analytics") as mock_clear,
            patch.object(metrics, "music_tempo") as mock_tempo,
            patch.object(metrics, "game_sensitivity") as mock_sens,
            patch.object(metrics, "effective_warning_threshold") as mock_warn,
            patch.object(metrics, "effective_death_threshold") as mock_death,
        ):
            metrics.clear_session_player_analytics(
                ["shadow_p1", "shadow_p2"], game_id="shadow_game", reset_global_gauges=False
            )

            # Targeted per-serial cleanup for exactly the shadow's serials.
            mock_clear.assert_any_call("shadow_p1", "shadow_game")
            mock_clear.assert_any_call("shadow_p2", "shadow_game")
            assert mock_clear.call_count == 2
            # Global single-game gauges untouched by the shadow end.
            mock_tempo.set.assert_not_called()
            mock_sens.set.assert_not_called()
            mock_warn.set.assert_not_called()
            mock_death.set.assert_not_called()

    def test_primary_end_resets_global_gauges(self):
        """The primary session's cleanup DOES reset the single-game gauges."""
        from services.game_coordinator import metrics

        with (
            patch.object(metrics, "clear_player_analytics") as mock_clear,
            patch.object(metrics, "music_tempo") as mock_tempo,
            patch.object(metrics, "game_sensitivity") as mock_sens,
            patch.object(metrics, "effective_warning_threshold") as mock_warn,
            patch.object(metrics, "effective_death_threshold") as mock_death,
        ):
            metrics.clear_session_player_analytics(["primary_p1"], game_id="primary_game", reset_global_gauges=True)

            mock_clear.assert_called_once_with("primary_p1", "primary_game")
            mock_tempo.set.assert_called_once_with(0)
            mock_sens.set.assert_called_once_with(0)
            mock_warn.set.assert_called_once_with(0)
            mock_death.set.assert_called_once_with(0)


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
