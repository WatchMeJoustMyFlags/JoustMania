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
from unittest.mock import ANY, patch

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


def _config(game_name="FFA", serials=("p1", "p2"), sensitivity=2, origin=None):
    # Default to MENU origin (#837): the characterization tests assert the legacy
    # "first start becomes primary" semantics, now reserved for the real
    # (menu-origin) game. Use _shadow_config() for agent/shadow starts.
    if origin is None:
        origin = game_coordinator_pb2.GAME_ORIGIN_MENU
    return game_coordinator_pb2.StartGameConfig(
        game_name=game_name,
        players=[game_coordinator_pb2.Player(serial=s) for s in serials],
        sensitivity=sensitivity,
        origin=origin,
    )


def _shadow_config(game_name="FFA", serials=("p1", "p2"), sensitivity=2):
    """A non-menu (AGENT) start that is ALWAYS classified shadow (#837)."""
    return _config(
        game_name=game_name,
        serials=serials,
        sensitivity=sensitivity,
        origin=game_coordinator_pb2.GAME_ORIGIN_AGENT,
    )


def _experiment_config(
    game_name="FFA",
    serials=("p1", "p2"),
    sensitivity=2,
    *,
    origin=game_coordinator_pb2.GAME_ORIGIN_AGENT,
    experiment_id="exp_abc123",
    arm="experimental",
):
    """A start bound to an experiment + arm at spawn (#976).

    Defaults to an AGENT (shadow) origin — the only place experiment binding is
    valid. Pass ``origin=GAME_ORIGIN_MENU`` to exercise the real-game invariant
    (the experiment fields MUST be dropped).
    """
    return game_coordinator_pb2.StartGameConfig(
        game_name=game_name,
        players=[game_coordinator_pb2.Player(serial=s) for s in serials],
        sensitivity=sensitivity,
        origin=origin,
        experiment_id=experiment_id,
        arm=arm,
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

    @pytest.fixture(autouse=True)
    def allow_shadows(self):
        """These tests run shadow + real games concurrently (the CI scenario),
        so the resource gate must be in ``allow`` mode (#837)."""
        with patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"):
            yield

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
        """A menu start (primary) + an agent start (shadow) get distinct ids/kinds."""
        with _NO_THREAD:
            ok1, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            ok2, gid2 = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())

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
            _, gid2 = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())

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
    @patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow")
    async def test_third_start_rejected_at_cap(self, servicer, cap_two):
        """With cap=2, a third shadow start is rejected once the cap is full.

        Uses one menu primary + two agent shadows so the third start does NOT
        preempt (only a real-game start preempts shadows, #837). Policy=allow so
        the gate admits shadows and the third hits the concurrency CAP, not the
        resource gate.
        """
        with _NO_THREAD:
            await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())
            ok3, error = await servicer._start_game_from_config(_shadow_config(serials=("c1", "c2")), _MockSpan())

        assert ok3 is False
        assert "already in progress" in error.lower()

    @pytest.mark.asyncio
    async def test_force_end_targets_primary_only(self, servicer, cap_two):
        """ForceEndGame (no game_id) ends the primary and frees the primary slot."""
        with _NO_THREAD:
            _, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            # Second concurrent session is a shadow (#1018: a second MENU start is
            # rejected by the real-game single-game guard).
            _, gid2 = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())

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


class TestConcurrentShadowGames:
    """Concurrent shadow games (#1018, option b).

    The single-game guard is scoped to REAL games only: N shadow games run at
    the same time (up to ``GAME_MAX_SHADOW_GAMES``), while a second REAL game is
    still rejected. These tests pin the caps explicitly so they don't depend on
    the module defaults.
    """

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.fixture(autouse=True)
    def allow_shadows(self):
        """Run shadows alongside a real game where needed; policy=allow."""
        with patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"):
            yield

    @pytest.mark.asyncio
    async def test_multiple_shadows_run_concurrently(self, servicer):
        """3 shadow games all reach RUNNING at the same time (no single-game guard
        among shadows). This is the core #1018 behavior — effective concurrency
        was 1 under #998's backpressure; now N shadows coexist."""
        with patch.dict(os.environ, {"GAME_MAX_SHADOW_GAMES": "4", "GAME_MAX_CONCURRENT_GAMES": "8"}):
            gids = []
            for i in range(3):
                gid, session = await _start_running(servicer, _shadow_config(serials=(f"a{i}", f"b{i}")))
                gids.append(gid)
                assert session.game_kind == "shadow"

        # All three distinct, all registered, all RUNNING simultaneously.
        assert len(set(gids)) == 3
        for gid in gids:
            assert gid in servicer.sessions
            assert servicer.sessions[gid].game_state == game_coordinator_pb2.GameState.RUNNING

    @pytest.mark.asyncio
    async def test_shadow_cap_rejects_beyond_limit(self, servicer):
        """A shadow start beyond GAME_MAX_SHADOW_GAMES is rejected (backpressure)."""
        with _NO_THREAD, patch.dict(os.environ, {"GAME_MAX_SHADOW_GAMES": "2", "GAME_MAX_CONCURRENT_GAMES": "8"}):
            ok1, _ = await servicer._start_game_from_config(_shadow_config(serials=("a1", "a2")), _MockSpan())
            ok2, _ = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())
            ok3, error = await servicer._start_game_from_config(_shadow_config(serials=("c1", "c2")), _MockSpan())

        assert ok1 and ok2
        assert ok3 is False
        assert "already in progress" in error.lower()

    @pytest.mark.asyncio
    async def test_second_real_game_rejected_while_shadows_run(self, servicer):
        """The single-game guard is REAL-only: two real games can't coexist even
        though many shadows can. The second MENU start is rejected."""
        with _NO_THREAD, patch.dict(os.environ, {"GAME_MAX_SHADOW_GAMES": "4", "GAME_MAX_CONCURRENT_GAMES": "8"}):
            ok1, gid1 = await servicer._start_game_from_config(_config(serials=("r1", "r2")), _MockSpan())
            # Shadows are fine alongside the real game.
            oks, _ = await servicer._start_game_from_config(_shadow_config(serials=("s1", "s2")), _MockSpan())
            # A second real game is rejected by the real-game single-game guard.
            ok2, error = await servicer._start_game_from_config(_config(serials=("r3", "r4")), _MockSpan())

        assert ok1 and oks
        assert servicer.sessions[gid1].game_kind == "primary"
        assert ok2 is False
        assert "already in progress" in error.lower()

    @pytest.mark.asyncio
    async def test_real_game_preempts_concurrent_shadows(self, servicer):
        """Real-vs-shadow policy: the real game takes precedence. An incoming real
        game preempts ALL live shadows so it gets the resources (#837), regardless
        of how many shadows were running concurrently (#1018)."""
        with patch.dict(os.environ, {"GAME_MAX_SHADOW_GAMES": "4", "GAME_MAX_CONCURRENT_GAMES": "8"}):
            shadow_gids = []
            for i in range(3):
                gid, _ = await _start_running(servicer, _shadow_config(serials=(f"s{i}", f"t{i}")))
                shadow_gids.append(gid)

            with _NO_THREAD:
                ok, real_gid = await servicer._start_game_from_config(_config(serials=("r1", "r2")), _MockSpan())

        assert ok is True
        # Every shadow was preempted/retired; only the real game remains.
        for gid in shadow_gids:
            assert gid not in servicer.sessions
        assert servicer.sessions[real_gid].game_kind == "primary"
        assert servicer._primary_game_id == real_gid

    @pytest.mark.asyncio
    async def test_concurrent_shadows_isolated_event_streams(self, servicer):
        """Two concurrent shadows do not cross-contaminate: each bus delivers only
        its own game's events (independent scoring/death/effect dispatch)."""
        with patch.dict(os.environ, {"GAME_MAX_SHADOW_GAMES": "4", "GAME_MAX_CONCURRENT_GAMES": "8"}):
            gid1, s1 = await _start_running(servicer, _shadow_config(serials=("a1", "a2")))
            gid2, s2 = await _start_running(servicer, _shadow_config(serials=("b1", "b2")))

        bus1, bus2 = s1.event_bus, s2.event_bus
        # Each shadow owns a DISTINCT EventBus instance (no shared mutable bus).
        assert bus1 is not bus2
        assert bus1 is not servicer.primary_event_bus
        assert bus2 is not servicer.primary_event_bus

        q1 = await bus1.subscribe("sub1")
        q2 = await bus2.subscribe("sub2")
        await bus1.publish("player_death", {"serial": "a1"})
        await bus2.publish("player_death", {"serial": "b1"})

        e1 = q1.get_nowait()
        e2 = q2.get_nowait()
        assert e1.game_id == gid1 and e1.data["serial"] == "a1"
        assert e2.game_id == gid2 and e2.data["serial"] == "b1"
        # Neither queue saw the other's death event (no cross-contamination).
        assert q1.empty()
        assert q2.empty()

    def test_shadow_cap_clamped_to_total_ceiling(self):
        """The shadow cap can never exceed the total ceiling, even if configured
        higher (the total ceiling is the hard runaway safety net)."""
        from services.game_coordinator.servicer import _max_shadow_games

        with patch.dict(os.environ, {"GAME_MAX_SHADOW_GAMES": "50", "GAME_MAX_CONCURRENT_GAMES": "3"}):
            assert _max_shadow_games() == 3


class TestGameIdRouting:
    """game_id routing in the proto + coordinator wiring (#776)."""

    @pytest.fixture(autouse=True)
    def allow_shadows(self):
        """Routing tests run concurrent sessions; keep the resource gate in
        ``allow`` mode so shadows are admitted alongside the primary (#837)."""
        with patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"):
            yield

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.fixture
    def cap_two(self):
        with patch.dict(os.environ, {"GAME_MAX_CONCURRENT_GAMES": "2"}):
            yield

    @pytest.mark.asyncio
    async def test_events_stamped_with_session_game_id(self, servicer, cap_two):
        """Each session's published events carry that session's game_id (#776).

        The headless starter learns its game_id from the stamped event, and a
        second concurrent session's events carry the OTHER id.
        """
        with _NO_THREAD:
            _, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            # Second concurrent session is a shadow (#1018: a second MENU start is
            # rejected by the real-game single-game guard).
            _, gid2 = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())

        bus1 = servicer.sessions[gid1].event_bus
        bus2 = servicer.sessions[gid2].event_bus

        q1 = await bus1.subscribe("sub1")
        q2 = await bus2.subscribe("sub2")
        await bus1.publish("player_death", {"serial": "a1"})
        await bus2.publish("player_death", {"serial": "b1"})

        e1 = q1.get_nowait()
        e2 = q2.get_nowait()
        # The GameEvent.game_id FIELD (not data map) is stamped per session.
        assert e1.game_id == gid1
        assert e2.game_id == gid2
        assert gid1 != gid2

    @pytest.mark.asyncio
    async def test_primary_bus_game_id_cleared_on_retire(self, servicer):
        """The persistent primary bus stamps the live game_id, then clears it.

        Because the primary bus outlives sessions, a game_id fixed at
        construction would be wrong — it must be set on bind and cleared on
        retire so post-retire idle events carry empty game_id.
        """
        _, _session = await _start_running(servicer, _config())
        gid = servicer.game_id
        assert servicer.primary_event_bus.current_game_id == gid

        q = await servicer.primary_event_bus.subscribe("sub")
        await servicer.primary_event_bus.publish("player_death", {"serial": "p1"})
        assert q.get_nowait().game_id == gid

        await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="done"), MockGrpcContext())
        # Primary slot freed and bus stamp cleared.
        assert servicer.primary_event_bus.current_game_id == ""
        # Drain any events queued during force-end (e.g. game_force_ended, still
        # stamped with gid because the stamp is cleared only after they publish).
        while not q.empty():
            q.get_nowait()
        # A NEW event published after retire carries empty game_id.
        await servicer.primary_event_bus.publish("ambient", {})
        assert q.get_nowait().game_id == ""

    @pytest.mark.asyncio
    async def test_subscribe_by_game_id_receives_only_that_game(self, servicer, cap_two):
        """StreamGameEvents(game_id=...) subscribes to exactly that game's bus."""
        with _NO_THREAD:
            _, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            # gid2 is a shadow (#1018: a second MENU start is rejected).
            _, gid2 = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())

        request = game_coordinator_pb2.StreamEventsRequest(game_id=gid2)
        context = MockGrpcContext()

        async def _publish():
            # Publish on both buses; only gid2's event should reach the stream.
            await servicer.sessions[gid1].event_bus.publish("player_death", {"serial": "a1"})
            await servicer.sessions[gid2].event_bus.publish("player_death", {"serial": "b1"})

        collected = await _stream_until_event(servicer, request, context, after=_publish)
        assert collected
        assert all(e.game_id == gid2 for e in collected)
        assert any(e.data.get("serial") == "b1" for e in collected)

    @pytest.mark.asyncio
    async def test_subscribe_unknown_game_id_yields_error_and_closes(self, servicer):
        """StreamGameEvents with an unknown game_id yields a game_error and closes."""
        request = game_coordinator_pb2.StreamEventsRequest(game_id="game_doesnotexist")
        context = MockGrpcContext()

        collected = [event async for event in servicer.StreamGameEvents(request, context)]

        assert len(collected) == 1
        assert collected[0].event_type == "game_error"
        assert "game_doesnotexist" in collected[0].data["error"]
        assert collected[0].game_id == "game_doesnotexist"

    @pytest.mark.asyncio
    async def test_force_end_by_game_id_targets_only_that_session(self, servicer, cap_two):
        """ForceEndGame(game_id=shadow) ends only the shadow, leaving the primary."""
        with _NO_THREAD:
            _, gid1 = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            _, gid2 = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())

            resp = await servicer.ForceEndGame(
                game_coordinator_pb2.ForceEndGameRequest(reason="test", game_id=gid2), MockGrpcContext()
            )

        assert resp.success is True
        # Shadow retired; primary untouched and still owns the primary slot.
        assert gid2 not in servicer.sessions
        assert gid1 in servicer.sessions
        assert servicer._primary_game_id == gid1

    @pytest.mark.asyncio
    async def test_force_end_unknown_game_id_returns_failure(self, servicer):
        """ForceEndGame with an unknown game_id returns success=False."""
        resp = await servicer.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="x", game_id="game_nope"), MockGrpcContext()
        )
        assert resp.success is False
        assert "game_nope" in resp.error

    @pytest.mark.asyncio
    async def test_get_game_state_by_game_id(self, servicer, cap_two):
        """GetGameState(game_id=...) returns that specific session's state."""
        gid1, _ = await _start_running(servicer, _config(game_name="FFA", serials=("a1", "a2")))
        # gid2 is a shadow (#1018: a second MENU start is rejected).
        gid2, _ = await _start_running(servicer, _shadow_config(game_name="Teams", serials=("b1", "b2")))

        resp1 = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(game_id=gid1), MockGrpcContext())
        resp2 = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(game_id=gid2), MockGrpcContext())
        assert resp1.game_info.game_id == gid1
        assert resp1.game_info.game_mode == "FFA"
        assert resp2.game_info.game_id == gid2
        assert resp2.game_info.game_mode == "Teams"

    @pytest.mark.asyncio
    async def test_get_game_state_unknown_game_id_returns_failure(self, servicer):
        """GetGameState with an unknown game_id returns success=False."""
        resp = await servicer.GetGameState(
            game_coordinator_pb2.GetGameStateRequest(game_id="game_nope"), MockGrpcContext()
        )
        assert resp.success is False
        assert "game_nope" in resp.error

    @pytest.mark.asyncio
    async def test_list_games_shows_running_then_empties(self, servicer, cap_two):
        """ListGames enumerates all live sessions, then empties as they end."""
        gid1, _ = await _start_running(servicer, _config(game_name="FFA", serials=("a1", "a2")))
        gid2, _ = await _start_running(servicer, _shadow_config(game_name="Teams", serials=("b1", "b2")))

        resp = await servicer.ListGames(game_coordinator_pb2.ListGamesRequest(), MockGrpcContext())
        assert resp.success is True
        ids = {g.game_id for g in resp.games}
        assert ids == {gid1, gid2}

        # End both; ListGames empties out.
        await servicer.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(reason="x", game_id=gid2), MockGrpcContext()
        )
        await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="x"), MockGrpcContext())

        resp_empty = await servicer.ListGames(game_coordinator_pb2.ListGamesRequest(), MockGrpcContext())
        assert resp_empty.success is True
        assert list(resp_empty.games) == []

    @pytest.mark.asyncio
    async def test_legacy_empty_game_id_requests_unchanged(self, servicer):
        """Zero-field requests behave exactly as before (#776 backward compat).

        Empty game_id on ForceEndGame/GetGameState resolves to the primary, and
        a zero-arg StreamGameEvents still subscribes to the primary bus.
        """
        gid, _ = await _start_running(servicer, _config(game_name="FFA"))

        # Empty-game_id GetGameState -> primary.
        state = await servicer.GetGameState(game_coordinator_pb2.GetGameStateRequest(), MockGrpcContext())
        assert state.game_info.game_id == gid

        # Zero-arg stream still receives the primary game's events.
        context = MockGrpcContext()

        async def _publish():
            await servicer.event_bus.publish("player_death", {"serial": "p1"})

        collected = await _stream_until_event(
            servicer, game_coordinator_pb2.StreamEventsRequest(), context, after=_publish
        )
        assert any(e.event_type == "player_death" for e in collected)

        # Empty-game_id ForceEndGame -> ends the primary.
        resp = await servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="done"), MockGrpcContext())
        assert resp.success is True
        assert servicer.game_state == game_coordinator_pb2.GameState.IDLE


class TestShadowGameGovernance:
    """Origin marking + resource gate + preemption (#837)."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.fixture
    def cap_two(self):
        with patch.dict(os.environ, {"GAME_MAX_CONCURRENT_GAMES": "2"}):
            yield

    @pytest.fixture
    def policy_block(self):
        with patch("services.game_coordinator.servicer._shadow_policy", lambda: "block"):
            yield

    @pytest.fixture
    def policy_allow(self):
        with patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"):
            yield

    # -- Origin classification ----------------------------------------------

    @pytest.mark.asyncio
    async def test_menu_origin_start_is_primary(self, servicer):
        """A GAME_ORIGIN_MENU start claims the primary slot + persistent bus."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
        assert ok is True
        session = servicer.sessions[gid]
        assert session.game_kind == "primary"
        assert servicer._primary_game_id == gid
        assert session.event_bus is servicer.primary_event_bus

    @pytest.mark.asyncio
    async def test_real_game_wins_admission_race_against_late_shadow(self, servicer, policy_allow):
        """#837 hardening: preemption runs outside the sessions lock, so a shadow
        can register between a real game's preemption pass and its admission.
        The real game must NOT be cap-rejected — it preempts again and retries.

        Simulated by making the first preemption pass a no-op (as if the shadow
        slipped in just after it ran): attempt 0 hits the full cap, attempt 1
        preempts for real and admits.

        Pins the total ceiling to 1 (#1018 default is 8) so a single live shadow
        fills the ceiling and the real game must preempt to win the slot.
        """
        # Total ceiling pinned to 1; a live shadow occupies the only slot.
        with _NO_THREAD, patch.dict(os.environ, {"GAME_MAX_CONCURRENT_GAMES": "1"}):
            ok, _ = await servicer._start_game_from_config(_shadow_config(serials=("s1", "s2")), _MockSpan())
            assert ok is True

        real_preempt = servicer._preempt_shadow_sessions
        calls = {"n": 0}

        async def flaky_preempt():
            calls["n"] += 1
            if calls["n"] == 1:
                return 0  # simulate the shadow registering after this pass
            return await real_preempt()

        servicer._preempt_shadow_sessions = flaky_preempt
        with _NO_THREAD, patch.dict(os.environ, {"GAME_MAX_CONCURRENT_GAMES": "1"}):
            ok, gid = await servicer._start_game_from_config(_config(serials=("r1", "r2")), _MockSpan())

        assert ok is True, f"real game lost the admission race: {gid}"
        assert calls["n"] == 2, "expected a second preemption pass after the cap rejection"
        assert servicer.sessions[gid].game_kind == "primary"
        assert servicer._primary_game_id == gid

    @pytest.mark.asyncio
    async def test_agent_origin_start_is_shadow_even_when_idle(self, servicer):
        """An AGENT start is ALWAYS shadow, even with NO primary live (idle)."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("a1", "a2")), _MockSpan())
        assert ok is True
        session = servicer.sessions[gid]
        # The first-come-first-served hazard is gone: idle-time agent game does
        # NOT claim primary.
        assert session.game_kind == "shadow"
        assert servicer._primary_game_id is None
        assert session.event_bus is not servicer.primary_event_bus

    @pytest.mark.asyncio
    async def test_unspecified_origin_start_is_shadow_when_idle(self, servicer):
        """OLD assumption gone: an UNSPECIFIED-origin start while idle is shadow.

        Previously the first start (any origin) became primary. Now only MENU may
        be primary; an unmarked start is a shadow even when the system is idle.
        """
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(
                _config(serials=("a1", "a2"), origin=game_coordinator_pb2.GAME_ORIGIN_UNSPECIFIED),
                _MockSpan(),
            )
        assert ok is True
        assert servicer.sessions[gid].game_kind == "shadow"
        assert servicer._primary_game_id is None

    @pytest.mark.asyncio
    @patch("services.game_coordinator.servicer.metrics")
    async def test_metrics_labeled_by_kind(self, mock_metrics, servicer):
        """Shadow starts label their lifecycle metrics game_kind='shadow'."""
        with _NO_THREAD:
            await servicer._start_game_from_config(_shadow_config(serials=("a1", "a2")), _MockSpan())
        mock_metrics.active_game.labels.assert_any_call(game_kind="shadow", game_id=ANY, experiment_id="", arm="")
        # games_started_total is a cumulative counter: no experiment_id/arm (#975).
        mock_metrics.games_started_total.labels.assert_any_call(mode="FFA", game_kind="shadow")

    # -- Resource gate ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_gate_blocks_shadow_while_real_running(self, servicer, cap_two, policy_block):
        """policy=block: a shadow start is rejected (distinct message) while a
        real game is STARTING/RUNNING."""
        with _NO_THREAD:
            _, gid_real = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            ok, error = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())

        assert ok is False
        # DISTINCT message — NOT the legacy "Game already in progress".
        assert error == "Shadow games blocked while a real game is running"
        assert "already in progress" not in error.lower()
        # The real game is untouched and still primary.
        assert servicer._primary_game_id == gid_real

    @pytest.mark.asyncio
    async def test_gate_allows_shadow_when_policy_allow(self, servicer, cap_two, policy_allow):
        """policy=allow: a shadow start is admitted alongside a real game (CI)."""
        with _NO_THREAD:
            _, gid_real = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
            ok, gid_shadow = await servicer._start_game_from_config(_shadow_config(serials=("b1", "b2")), _MockSpan())

        assert ok is True
        assert servicer.sessions[gid_shadow].game_kind == "shadow"
        assert servicer._primary_game_id == gid_real

    @pytest.mark.asyncio
    async def test_gate_does_not_block_shadow_when_idle(self, servicer, policy_block):
        """policy=block but NO real game running -> shadow start admitted."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("a1", "a2")), _MockSpan())
        assert ok is True
        assert servicer.sessions[gid].game_kind == "shadow"

    # -- Preemption ---------------------------------------------------------

    @pytest.mark.asyncio
    async def test_real_start_preempts_live_shadow(self, servicer, cap_two, policy_allow):
        """A real (menu) start force-ends running shadows and becomes primary."""
        # A shadow is running idle-time.
        gid_shadow, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        assert shadow.game_kind == "shadow"

        # Subscribe to the shadow's bus to observe the preemption notice.
        q = await shadow.event_bus.subscribe("watch")

        with _NO_THREAD:
            ok, gid_real = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())

        assert ok is True
        # Shadow was force-ended + retired; real game is the new primary.
        assert gid_shadow not in servicer.sessions
        assert servicer.sessions[gid_real].game_kind == "primary"
        assert servicer._primary_game_id == gid_real

        # The shadow's bus received game_force_ended with the preemption reason.
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        force_ended = [e for e in events if e.event_type == "game_force_ended"]
        assert force_ended, f"expected game_force_ended, saw {[e.event_type for e in events]}"
        assert force_ended[0].data["reason"] == "preempted_by_real_game"

    @pytest.mark.asyncio
    async def test_real_start_preempts_even_when_policy_block(self, servicer, cap_two, policy_block):
        """Preemption applies regardless of policy — the real game always wins.

        Even though policy=block (which would reject a NEW shadow), an already
        running shadow is preempted so the real game starts.
        """
        gid_shadow, _ = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        with _NO_THREAD:
            ok, gid_real = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
        assert ok is True
        assert gid_shadow not in servicer.sessions
        assert servicer.sessions[gid_real].game_kind == "primary"

    @pytest.mark.asyncio
    async def test_real_start_not_rejected_when_shadows_fill_cap(self, servicer, cap_two, policy_allow):
        """Freed shadow slots count before the cap check — the real game (cap=2)
        is never rejected because shadows filled the cap (#837)."""
        # Fill BOTH slots with shadows.
        await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        await _start_running(servicer, _shadow_config(serials=("s3", "s4")))
        assert len(servicer.sessions) == 2

        with _NO_THREAD:
            ok, gid_real = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())

        assert ok is True
        assert servicer.sessions[gid_real].game_kind == "primary"
        # Both shadows were preempted to make room.
        assert len(servicer.sessions) == 1

    @pytest.mark.asyncio
    async def test_shadow_start_does_not_preempt_other_shadow(self, servicer, cap_two, policy_allow):
        """A shadow start must NOT preempt another shadow (only real games do)."""
        gid1, _ = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        with _NO_THREAD:
            ok, gid2 = await servicer._start_game_from_config(_shadow_config(serials=("s3", "s4")), _MockSpan())
        assert ok is True
        # Both shadows coexist.
        assert gid1 in servicer.sessions
        assert servicer.sessions[gid1].game_kind == "shadow"
        assert servicer.sessions[gid2].game_kind == "shadow"

    @pytest.mark.asyncio
    async def test_idle_shadow_preempted_by_menu_becomes_primary(self, servicer, cap_two, policy_block):
        """End-to-end: an agent game runs as shadow while idle; a menu start
        preempts it and becomes primary (the canonical #837 flow)."""
        gid_shadow, _ = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        assert servicer.sessions[gid_shadow].game_kind == "shadow"
        assert servicer._primary_game_id is None  # shadow never claimed primary

        gid_real, real = await _start_running(servicer, _config(serials=("a1", "a2")))
        assert gid_shadow not in servicer.sessions
        assert real.game_kind == "primary"
        assert servicer._primary_game_id == gid_real


class TestExperimentSpawnBinding:
    """Spawn binding of (experiment_id, arm) at game start (#976, epic #982).

    A shadow start may carry an experiment + arm on its StartGameConfig; the
    servicer binds them onto the started GameSession AT SPAWN so the session's
    eval-context (targeting #977) and telemetry (attribution #975) carry them
    from the start. A real game — or a shadow game with no experiment fields —
    carries none. The HARD INVARIANT (#982): experiment fields only SUBDIVIDE a
    shadow game; a real (primary) game can never carry an experiment.
    """

    @pytest.fixture(autouse=True)
    def allow_shadows(self):
        with patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"):
            yield

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    async def test_shadow_start_binds_experiment_and_arm(self, servicer):
        """A shadow start carrying (experiment_id, arm) binds them on the session
        AND keeps game_kind="shadow" — experiment fields SUBDIVIDE shadow, never
        replace the real/shadow safety bit (#982 hard invariant)."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(
                _experiment_config(serials=("a1", "a2"), experiment_id="exp_abc123", arm="experimental"),
                _MockSpan(),
            )
        assert ok is True
        session = servicer.sessions[gid]
        assert session.game_kind == "shadow"  # HARD INVARIANT: still a shadow game
        assert session.experiment_id == "exp_abc123"
        assert session.arm == "experimental"

    @pytest.mark.asyncio
    async def test_shadow_control_arm_binds(self, servicer):
        """The control arm binds the same way and stays game_kind="shadow"
        (control games are NOT a separate kind — #982 resolved decision)."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(
                _experiment_config(serials=("a1", "a2"), experiment_id="exp_abc123", arm="control"),
                _MockSpan(),
            )
        assert ok is True
        session = servicer.sessions[gid]
        assert session.game_kind == "shadow"
        assert session.experiment_id == "exp_abc123"
        assert session.arm == "control"

    @pytest.mark.asyncio
    async def test_shadow_without_experiment_carries_none(self, servicer):
        """A shadow game NOT bound to an experiment carries empty experiment_id/arm
        (real-by-default for the experiment dimension)."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("a1", "a2")), _MockSpan())
        assert ok is True
        session = servicer.sessions[gid]
        assert session.game_kind == "shadow"
        assert session.experiment_id == ""
        assert session.arm == ""

    @pytest.mark.asyncio
    async def test_real_start_carries_no_experiment(self, servicer):
        """A real (menu-origin/primary) game carries no experiment fields even
        when none were provided — the baseline real-by-default case."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())
        assert ok is True
        session = servicer.sessions[gid]
        assert session.game_kind == "primary"
        assert session.experiment_id == ""
        assert session.arm == ""

    @pytest.mark.asyncio
    async def test_real_start_drops_experiment_fields_invariant(self, servicer):
        """HARD INVARIANT (#982): a menu-origin (real/primary) start that
        ERRONEOUSLY carries experiment fields must have them DROPPED — a real
        game can never be constructed as part of an experiment, structurally."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(
                _experiment_config(
                    serials=("a1", "a2"),
                    origin=game_coordinator_pb2.GAME_ORIGIN_MENU,
                    experiment_id="exp_should_not_apply",
                    arm="experimental",
                ),
                _MockSpan(),
            )
        assert ok is True
        session = servicer.sessions[gid]
        assert session.game_kind == "primary"
        # The invariant: the real game dropped the experiment fields entirely.
        assert session.experiment_id == ""
        assert session.arm == ""

    @pytest.mark.asyncio
    @patch("services.game_coordinator.servicer.metrics")
    async def test_bound_experiment_labels_live_gauges(self, mock_metrics, servicer):
        """The spawn binding feeds the per-live-game GAUGE labels (#975) so an
        experiment game's telemetry is attributed to its cohort from the start."""
        with _NO_THREAD:
            await servicer._start_game_from_config(
                _experiment_config(serials=("a1", "a2"), experiment_id="exp_abc123", arm="control"),
                _MockSpan(),
            )
        mock_metrics.active_game.labels.assert_any_call(
            game_kind="shadow", game_id=ANY, experiment_id="exp_abc123", arm="control"
        )
        mock_metrics.active_players.labels.assert_any_call(
            game_kind="shadow", game_id=ANY, experiment_id="exp_abc123", arm="control"
        )

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.set_game_session_kind_context")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_bound_experiment_set_on_eval_context_before_game_init(
        self, mock_tracer_mod, mock_factory, mock_set_kind, mock_metrics, servicer
    ):
        """A spawned experiment session establishes experiment_id/arm on its
        eval-context (via set_game_session_kind_context) BEFORE the game mode's
        __init__ calibration reads — the same boundary as game_kind (#975/#977).
        Asserted via the call ordering against GameFactory.create_game()."""
        from unittest.mock import AsyncMock, MagicMock

        order = []
        mock_set_kind.side_effect = lambda kind, **kw: order.append(("set_kind", kind, kw))

        # Build a real shadow session through the servicer spawn path, then drive
        # its loop directly (the servicer's start_thread is what spins the loop;
        # we run the async loop body inline to observe the eval-context ordering).
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(
                _experiment_config(serials=("a1", "a2"), experiment_id="exp_abc123", arm="experimental"),
                _MockSpan(),
            )
        assert ok is True
        session = servicer.sessions[gid]

        # The session-internal tracer/factory are patched at the module level, so
        # the loop body runs without a real game.
        from services.game_coordinator.tests.test_game_session import _tracer_mock

        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()

        def _record_create(*_a, **_k):
            order.append(("create_game", None, None))
            return mock_game

        mock_factory.create_game.side_effect = _record_create

        await session._run_game_loop_async()

        # set_game_session_kind_context was called with the experiment identity ...
        kind_calls = [c for c in order if c[0] == "set_kind"]
        assert kind_calls, "set_game_session_kind_context was never called"
        kind, kwargs = kind_calls[0][1], kind_calls[0][2]
        assert kind == "shadow"
        assert kwargs.get("experiment_id") == "exp_abc123"
        assert kwargs.get("arm") == "experimental"
        # ... and BEFORE the game mode was created (init-frozen reads).
        assert order.index(kind_calls[0]) < order.index(("create_game", None, None))


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
