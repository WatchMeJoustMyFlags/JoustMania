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
import logging
import sys
import threading
from pathlib import Path
from unittest.mock import ANY, AsyncMock, call, create_autospec, patch

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
from services.game_coordinator import metrics
from services.game_coordinator.games.base import BaseGameMode
from services.game_coordinator.interventions import InterventionContext
from services.game_coordinator.servicer import GameCoordinatorServicer

# Patch target: stops the real game thread from spinning up while still
# registering the session and publishing game_start. Tests drive RUNNING via the
# session's EventBus (publish GAME_STARTED) rather than mutating state directly,
# so they are agnostic to the servicer's internal structure.
_NO_THREAD = patch.object(game_session_mod.GameSession, "start_thread", lambda _self: None)


def _caps(*, concurrent: int | None = None, shadow: int | None = None):
    """Patch the game-flag-backed session caps for the duration of a test (#1215).

    The total/shadow ceilings moved from ``GAME_MAX_CONCURRENT_GAMES`` /
    ``GAME_MAX_SHADOW_GAMES`` env vars to the ``max_concurrent_games`` /
    ``max_shadow_games`` game flags, read via ``lib.feature_flags.read_int_flag``
    inside ``servicer._max_concurrent_games`` / ``_max_shadow_games`` (which import
    it lazily). Patching ``read_int_flag`` exercises the real clamp logic while
    pinning the resolved flag value, exactly as the old env-dict patch did for the
    env read.
    """
    import lib.feature_flags as ff

    def _fake_read_int_flag(_domain, flag_key, default, _game_id=None):
        if flag_key == "max_concurrent_games" and concurrent is not None:
            return concurrent
        if flag_key == "max_shadow_games" and shadow is not None:
            return shadow
        return default

    return patch.object(ff, "read_int_flag", _fake_read_int_flag)


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

    async def send_initial_metadata(self, _metadata):
        """The stream servicers send headers once subscribed (#1613)."""
        self.initial_metadata_sent = True


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


async def _collect_stream_events(servicer, request, context, *, until, after=None, deadline_s=2.0):
    """Run StreamGameEvents and collect events until one of type ``until`` arrives.

    ``after`` (an async callable) runs once the stream had a moment to start.
    Returns every collected event, in order (stops at the deadline otherwise).
    """
    gen = servicer.StreamGameEvents(request, context)
    collected = []

    async def _drain():
        async for event in gen:
            collected.append(event)
            if event.event_type == until or event.event_type == "game_start_error":
                break

    drain_task = asyncio.create_task(_drain())
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
        """StreamGameEvents(start_config) starts a game and streams all its events.

        The starter is subscribed before ``game_start`` is published (#1783), so
        the stream begins with ``game_start`` (carrying the game_id) and then
        sees later events, here a ``player_death``.
        """
        request = game_coordinator_pb2.StreamEventsRequest(start_config=_config())
        context = MockGrpcContext()

        async def _publish_followup():
            # The just-started session is the primary; publish on the primary bus
            # the stream subscribed to.
            await servicer.event_bus.publish("player_death", {"serial": "p1"})

        with _NO_THREAD:
            collected = await _collect_stream_events(
                servicer, request, context, until="player_death", after=_publish_followup
            )

        # The game must have actually started (id recorded on the servicer).
        assert servicer.game_id is not None and servicer.game_id.startswith("game_")
        assert [e.event_type for e in collected] == ["game_start", "player_death"]
        assert collected[0].data["game_id"] == servicer.game_id

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
                _config(game_name="Teams", serials=("p3", "p4", "p5")), _MockSpan()
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
        with _caps(concurrent=2):
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
    @pytest.mark.nfr("NFR-018")
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
    @pytest.mark.uc("UC-027", flow="main", steps=[2], br=["BR-001"])
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

    def test_shadow_end_clears_only_its_serials_and_game_state_series(self):
        """Cross-wipe fix: a shadow session's cleanup is targeted, not global.

        ``clear_session_player_analytics`` for a shadow session removes ONLY the
        shadow's serials and the shadow's own (game_kind, game_id) game-state
        series (FR-041), so the live primary game's series survive.
        """
        from services.game_coordinator import metrics

        test_thread = threading.current_thread()
        state_calls_here = []

        def record_state_call(*args):
            if threading.current_thread() is test_thread:
                state_calls_here.append(call(*args))

        with (
            patch.object(metrics, "clear_player_analytics") as mock_clear,
            patch.object(metrics, "clear_game_state_gauges", side_effect=record_state_call),
        ):
            metrics.clear_session_player_analytics(
                ["shadow_p1", "shadow_p2"], game_id="shadow_game", game_kind="shadow"
            )
            # The order that flaked CI (#1827): an earlier test's retire-metrics
            # timer removed its primary game's series inside this window. Only
            # this cleanup's own calls count.
            leaked_removal = threading.Thread(
                target=metrics.clear_game_state_gauges,
                args=("primary", "game_other"),
                name="retire-metrics-game_other",
            )
            leaked_removal.start()
            leaked_removal.join()

            # Targeted per-serial cleanup for exactly the shadow's serials.
            mock_clear.assert_any_call("shadow_p1", "shadow_game")
            mock_clear.assert_any_call("shadow_p2", "shadow_game")
            assert mock_clear.call_count == 2
            assert state_calls_here == [call("shadow", "shadow_game")]

    def test_primary_end_removes_its_game_state_series(self):
        """The primary session's cleanup removes its own game-state series."""
        from services.game_coordinator import metrics

        with (
            patch.object(metrics, "clear_player_analytics") as mock_clear,
            patch.object(metrics, "clear_game_state_gauges") as mock_state,
        ):
            metrics.clear_session_player_analytics(["primary_p1"], game_id="primary_game", game_kind="primary")

            mock_clear.assert_called_once_with("primary_p1", "primary_game")
            mock_state.assert_called_once_with("primary", "primary_game")


@pytest.mark.nfr("NFR-018")
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
        with _caps(concurrent=8, shadow=4):
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
        with _NO_THREAD, _caps(concurrent=8, shadow=2):
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
        with _NO_THREAD, _caps(concurrent=8, shadow=4):
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
        with _caps(concurrent=8, shadow=4):
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
        with _caps(concurrent=8, shadow=4):
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

        with _caps(concurrent=3, shadow=50):
            assert _max_shadow_games() == 3


@pytest.mark.nfr("NFR-018")
class TestSessionCapFlags:
    """The total/shadow session caps read from game.json flags (#1215).

    Migrated from the GAME_MAX_CONCURRENT_GAMES / GAME_MAX_SHADOW_GAMES env vars to
    the ``max_concurrent_games`` / ``max_shadow_games`` game flags. These tests pin
    the flag-read path AND the fail-open-to-default behaviour when flagd is
    unreachable/undefined at startup (``read_int_flag`` returns the supplied
    hardcoded default)."""

    def test_concurrent_reads_max_concurrent_games_flag(self):
        from services.game_coordinator.servicer import _max_concurrent_games

        with _caps(concurrent=12):
            assert _max_concurrent_games() == 12

    def test_concurrent_floors_at_one(self):
        """A nonsensical sub-1 flag value is floored to 1."""
        from services.game_coordinator.servicer import _max_concurrent_games

        with _caps(concurrent=0):
            assert _max_concurrent_games() == 1

    def test_shadow_reads_max_shadow_games_flag(self):
        from services.game_coordinator.servicer import _max_shadow_games

        with _caps(concurrent=8, shadow=2):
            assert _max_shadow_games() == 2

    def test_concurrent_fails_open_to_default(self):
        """When the flag read yields its default (flagd unreachable/undefined at
        startup, #1177), the env-era default DEFAULT_MAX_CONCURRENT_GAMES holds."""
        from services.game_coordinator.servicer import (
            DEFAULT_MAX_CONCURRENT_GAMES,
            _max_concurrent_games,
        )

        # read_int_flag returns its `default` arg on any failure; simulate that by
        # echoing the caller-supplied default back unchanged.
        with patch("lib.feature_flags.read_int_flag", lambda _d, _k, default, _game_id=None: default):
            assert _max_concurrent_games() == DEFAULT_MAX_CONCURRENT_GAMES

    def test_shadow_fails_open_to_default(self):
        """Shadow cap fails open to DEFAULT_MAX_SHADOW_GAMES (clamped to the total
        ceiling, which also falls open to its own default)."""
        from services.game_coordinator.servicer import (
            DEFAULT_MAX_CONCURRENT_GAMES,
            DEFAULT_MAX_SHADOW_GAMES,
            _max_shadow_games,
        )

        with patch("lib.feature_flags.read_int_flag", lambda _d, _k, default, _game_id=None: default):
            assert _max_shadow_games() == min(DEFAULT_MAX_SHADOW_GAMES, DEFAULT_MAX_CONCURRENT_GAMES)


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
        with _caps(concurrent=2):
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
    @pytest.mark.uc("UC-027", flow="main", steps=[2], br=["BR-001"])
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
    @pytest.mark.uc("UC-027", flow="A1")
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
        gid2, _ = await _start_running(servicer, _shadow_config(game_name="Teams", serials=("b1", "b2", "b3")))

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
        gid2, _ = await _start_running(servicer, _shadow_config(game_name="Teams", serials=("b1", "b2", "b3")))

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


class _BlockingGameThread:
    """Stands in for a shadow session's live game thread (#1816).

    ``GameSession.force_end`` sees it alive and joins it in the executor; the join
    blocks until the test sets ``release``, so the real force_end is suspended at
    its join exactly as with a game thread that takes its 2 s to stop.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.release = threading.Event()
        self.entered = asyncio.Event()
        self.joins = 0

    def is_alive(self) -> bool:
        return not self.release.is_set()

    def join(self, timeout: float | None = None) -> None:
        self.joins += 1
        self._loop.call_soon_threadsafe(self.entered.set)
        self.release.wait(timeout)


def _give_blocking_thread(session) -> _BlockingGameThread:
    thread = _BlockingGameThread(asyncio.get_running_loop())
    session.game_thread = thread
    return thread


class TestShadowGameGovernance:
    """Origin marking + resource gate + preemption (#837)."""

    @pytest.fixture
    def servicer(self):
        servicer = GameCoordinatorServicer()
        yield servicer
        # #1816, #1827: no session end task outlives its test.
        assert not servicer._session_endings

    @pytest.fixture
    def cap_two(self):
        with _caps(concurrent=2):
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
        with _NO_THREAD, _caps(concurrent=1):
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
        with _NO_THREAD, _caps(concurrent=1):
            ok, gid = await servicer._start_game_from_config(_config(serials=("r1", "r2")), _MockSpan())

        assert ok is True, f"real game lost the admission race: {gid}"
        # Pass 1 (no-op), pass 2 after the cap rejection, pass 3 after the admission.
        assert calls["n"] == 3, "expected a second preemption pass after the cap rejection"
        assert servicer.sessions[gid].game_kind == "primary"
        assert servicer._primary_game_id == gid

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-043", flow="A2")
    @pytest.mark.uc("UC-046", flow="A8")
    async def test_shadow_admitted_during_real_preemption_is_preempted(self, servicer):
        """#1404: a shadow start that lands while a real start runs its preemption pass
        (outside the lock, before the primary slot is live) passes the block gate and
        the cap. The real start preempts it after its own admission, and under
        shadow_policy=block the next shadow start is refused."""
        real_preempt = servicer._preempt_shadow_sessions
        late: dict[str, str] = {}

        async def preempt_while_a_shadow_starts():
            preempted = await real_preempt()
            if not late:
                ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("s1", "s2")), _MockSpan())
                assert ok is True, gid
                late["gid"] = gid
            return preempted

        servicer._preempt_shadow_sessions = preempt_while_a_shadow_starts
        with _NO_THREAD, patch("services.game_coordinator.servicer._shadow_policy", lambda: "block"):
            ok, gid = await servicer._start_game_from_config(_config(serials=("r1", "r2")), _MockSpan())
            assert ok is True, gid
            assert late, "the late shadow never started"
            assert late["gid"] not in servicer.sessions, "the shadow admitted during the real start still runs"
            assert set(servicer.sessions) == {gid}
            ok, error = await servicer._start_game_from_config(_shadow_config(serials=("s3", "s4")), _MockSpan())
        assert ok is False
        assert error == "Shadow games blocked while a real game is running"

    # -- Preemption survives a cancelled real start (#1816) ------------------

    @staticmethod
    def _drain(queue):
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return [e for e in events if e.event_type == "game_force_ended"]

    @staticmethod
    def _record_retires(servicer, game_id):
        retired = asyncio.Event()
        real_retire = servicer._retire_session

        def retire(gid):
            real_retire(gid)
            if gid == game_id:
                retired.set()

        servicer._retire_session = retire
        return retired

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-043", flow="A2")
    @pytest.mark.uc("UC-046", flow="A8")
    @pytest.mark.nfr("NFR-018")
    async def test_cancelled_real_start_still_retires_the_shadow_it_preempted(self, servicer):
        """#1816: a cancel of the real start while the shadow's force end joins its
        thread (first pass, before admission) does not stop the preemption: the
        shadow is force-ended, publishes game_force_ended and frees its cap slot."""
        with _caps(concurrent=8, shadow=1), patch.object(metrics, "shadow_games_preempted_total") as preempted:
            gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
            thread = _give_blocking_thread(shadow)
            q = await shadow.event_bus.subscribe("watch")
            retired = self._record_retires(servicer, gid_s)

            with _NO_THREAD:
                start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
                async with asyncio.timeout(2.0):
                    await thread.entered.wait()
                assert servicer._primary_game_id is None
                start.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await start
            assert gid_s in servicer.sessions  # the join is still blocked

            thread.release.set()
            async with asyncio.timeout(2.0):
                await retired.wait()

            assert gid_s not in servicer.sessions
            force_ended = self._drain(q)
            assert [e.data["reason"] for e in force_ended] == ["preempted_by_real_game"]
            preempted.inc.assert_called_once_with()
            assert servicer._flag_service_live_games() == []
            # The shadow-cap slot (cap 1) is free again.
            with _NO_THREAD:
                ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("s3", "s4")), _MockSpan())
            assert ok is True, gid
        await asyncio.sleep(0)  # the preemption task's done callback runs (leak guard)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-043", flow="A2")
    @pytest.mark.uc("UC-046", flow="A8")
    @pytest.mark.nfr("NFR-018")
    async def test_cancelled_real_start_after_admission_still_retires_the_shadow(self, servicer):
        """#1816 twin: the cancel lands in the pass after admission (#1404); the
        admitted primary is aborted (UC-010 BR-007) and the shadow is still retired."""
        real_preempt = servicer._preempt_shadow_sessions
        late: dict = {}
        late_started = asyncio.Event()

        async def preempt_while_a_shadow_starts():
            preempted = await real_preempt()
            if not late:
                ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("s1", "s2")), _MockSpan())
                assert ok is True, gid
                late["gid"], late["session"] = gid, servicer.sessions[gid]
                await late["session"].event_bus.publish(GameEvent.GAME_STARTED, {"game_id": gid})
                late["thread"] = _give_blocking_thread(late["session"])
                late["queue"] = await late["session"].event_bus.subscribe("watch")
                late["retired"] = self._record_retires(servicer, late["gid"])
                late_started.set()
            return preempted

        servicer._preempt_shadow_sessions = preempt_while_a_shadow_starts
        with _caps(concurrent=8, shadow=1), _NO_THREAD:
            start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
            async with asyncio.timeout(2.0):
                await late_started.wait()
                await late["thread"].entered.wait()
            gid_real = servicer._primary_game_id
            assert gid_real is not None
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
            assert gid_real not in servicer.sessions
            assert late["gid"] in servicer.sessions

            late["thread"].release.set()
            async with asyncio.timeout(2.0):
                await late["retired"].wait()

            assert set(servicer.sessions) == set()
            assert servicer._primary_game_id is None
            assert [e.data["reason"] for e in self._drain(late["queue"])] == ["preempted_by_real_game"]
            ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("s3", "s4")), _MockSpan())
            assert ok is True, gid
        await asyncio.sleep(0)  # the preemption task's done callback runs (leak guard)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-043", flow="A2")
    async def test_shadow_force_end_error_does_not_stop_the_real_start(self, servicer, caplog):
        """#1816: a shadow whose force end raises is logged and counted as not
        preempted; the real start goes ahead, and the ENDED sweep removes the
        shadow once its game has ended."""
        gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))

        async def _force_end_fails(_reason):
            raise RuntimeError("join failed")

        shadow.force_end = _force_end_fails
        with (
            _NO_THREAD,
            patch.object(metrics, "shadow_games_preempted_total") as preempted,
            patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"),
            caplog.at_level(logging.ERROR, logger="services.game_coordinator.servicer"),
        ):
            ok, gid_real = await servicer._start_game_from_config(_config(), _MockSpan())
            assert ok is True, gid_real
            assert servicer._primary_game_id == gid_real
            assert any(f"Failed to preempt shadow game {gid_s}" in r.getMessage() for r in caplog.records)
            preempted.inc.assert_not_called()
            assert gid_s in servicer.sessions

            # The shadow's thread ends its game; the next admission sweeps it.
            with shadow._state_lock:
                shadow.game_state = game_coordinator_pb2.GameState.ENDED
            ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("s3", "s4")), _MockSpan())
            assert ok is True, gid
        assert gid_s not in servicer.sessions

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-043", flow="A2")
    @pytest.mark.uc("UC-046", flow="A8")
    async def test_second_real_start_reuses_the_preemption_under_way(self, servicer):
        """#1816: a real start that follows a cancelled one while the shadow's force
        end still joins waits for that preemption: one force end, one
        game_force_ended, one count, and the second start is admitted."""
        with _caps(concurrent=8, shadow=1), patch.object(metrics, "shadow_games_preempted_total") as preempted:
            gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
            thread = _give_blocking_thread(shadow)
            q = await shadow.event_bus.subscribe("watch")

            with _NO_THREAD:
                first = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
                async with asyncio.timeout(2.0):
                    await thread.entered.wait()
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                _shadow, first_preemption = servicer._session_endings[gid_s]

                second_entered = asyncio.Event()
                real_preempt = servicer._preempt_shadow_sessions

                async def preempt():
                    second_entered.set()
                    return await real_preempt()

                servicer._preempt_shadow_sessions = preempt
                second = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
                async with asyncio.timeout(2.0):
                    await second_entered.wait()
                # The second start now waits on the preemption; let the join end.
                thread.release.set()
                async with asyncio.timeout(2.0):
                    ok, gid_real = await second
                    # Every preemption of the shadow has finished.
                    assert await first_preemption == (True, "")

            assert ok is True, gid_real
            assert servicer._primary_game_id == gid_real
            assert gid_s not in servicer.sessions
            assert thread.joins == 1
            assert [e.data["reason"] for e in self._drain(q)] == ["preempted_by_real_game"]
            preempted.inc.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-043", flow="A2")
    async def test_preemption_does_not_retire_a_shadow_the_ended_sweep_retired(self, servicer):
        """#1816: the shadow's thread ends the game naturally while its preemption
        joins it, and the next start's ENDED sweep retires it first; the
        preemption completing afterwards does not end it at the flag service again."""
        with _caps(concurrent=8, shadow=1):
            gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
            thread = _give_blocking_thread(shadow)

            with _NO_THREAD, patch.object(servicer.flag_service, "end") as flag_end:
                start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
                async with asyncio.timeout(2.0):
                    await thread.entered.wait()
                start.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await start
                _shadow, pending = servicer._session_endings[gid_s]

                # The game thread ends the game itself (natural end) ...
                with shadow._state_lock:
                    shadow.game_state = game_coordinator_pb2.GameState.ENDED
                # ... and the next start's sweep retires it.
                ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("s3", "s4")), _MockSpan())
                assert ok is True, gid
                assert gid_s not in servicer.sessions

                thread.release.set()
                async with asyncio.timeout(2.0):
                    assert await pending == (True, "")

            assert [c for c in flag_end.call_args_list if c.args == (gid_s,)] == [call(gid_s)]
            assert gid in servicer.sessions

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-043", flow="A2")
    async def test_shutdown_waits_for_a_pending_preemption(self, servicer):
        """#1816: shutdown lets a preemption whose real start was cancelled finish
        before it stops the flag-service lifecycle, so the shadow's end is queued
        while the lifecycle still runs."""
        gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        thread = _give_blocking_thread(shadow)
        registered_at_stop = []

        async def stop():
            registered_at_stop.append(gid_s in servicer.sessions)

        with _NO_THREAD:
            start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
            async with asyncio.timeout(2.0):
                await thread.entered.wait()
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start

        with patch.object(servicer.flag_service, "stop", stop):
            shutdown = asyncio.create_task(servicer.shutdown())
            await asyncio.sleep(0)  # shutdown runs up to its first await
            thread.release.set()
            async with asyncio.timeout(5.0):
                await shutdown

        assert registered_at_stop == [False]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-053", flow="main", steps=[7])
    async def test_shutdown_sends_end_queued_while_joining_before_stop(self, servicer):
        """#1849: a game that ends while shutdown joins its thread queues its
        flag-service end; the lifecycle flushes it before it stops."""
        gid, session = await _start_running(servicer, _config())
        order = []

        async def join_thread(*_args, **_kwargs):
            servicer._on_session_game_ended(session)  # the game thread's end hook
            order.append("joined")

        async def flush(*_args, **_kwargs):
            order.append("flush")

        async def stop():
            order.append("stop")

        with (
            patch.object(session, "join_thread", join_thread),
            patch.object(servicer.flag_service, "end", lambda game_id: order.append(f"end:{game_id}")),
            patch.object(servicer.flag_service, "flush", flush),
            patch.object(servicer.flag_service, "stop", stop),
        ):
            async with asyncio.timeout(5.0):
                await servicer.shutdown()

        assert order == [f"end:{gid}", "joined", "flush", "stop"]

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

    @pytest.mark.uc("UC-043", flow="A2")
    @pytest.mark.asyncio
    @pytest.mark.nfr("NFR-018")
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
    @pytest.mark.nfr("NFR-018")
    async def test_gate_does_not_block_shadow_when_idle(self, servicer, policy_block):
        """policy=block but NO real game running -> shadow start admitted."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("a1", "a2")), _MockSpan())
        assert ok is True
        assert servicer.sessions[gid].game_kind == "shadow"

    # -- Preemption ---------------------------------------------------------

    @pytest.mark.uc("UC-043", flow="A2")
    @pytest.mark.asyncio
    @pytest.mark.nfr("NFR-018")
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

    @pytest.mark.uc("UC-043", flow="A2")
    @pytest.mark.asyncio
    @pytest.mark.nfr("NFR-018")
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
    @pytest.mark.nfr("NFR-018")
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
    @pytest.mark.uc("UC-010", flow="A3")
    async def test_real_start_rejected_when_capacity_stays_full(self, servicer, cap_two, policy_allow):
        """UC-010 A3 "capacity reached": when the cap is still full after both preemption
        passes (a late shadow wins the admission race twice), the real start is rejected
        with the "Game already in progress" error and no primary is registered."""
        gid1, _ = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        gid2, _ = await _start_running(servicer, _shadow_config(serials=("s3", "s4")))
        preempt_calls = 0

        async def preemption_that_frees_nothing():
            nonlocal preempt_calls
            preempt_calls += 1
            return 0

        with (
            _NO_THREAD,
            patch.object(servicer, "_preempt_shadow_sessions", side_effect=preemption_that_frees_nothing),
        ):
            ok, error = await servicer._start_game_from_config(_config(serials=("a1", "a2")), _MockSpan())

        assert ok is False
        assert error == "Game already in progress"
        assert preempt_calls == 2  # first pass + the admission-race retry
        assert set(servicer.sessions) == {gid1, gid2}
        assert servicer._primary_game_id is None

    @pytest.mark.asyncio
    @pytest.mark.nfr("NFR-018")
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

    @pytest.mark.uc("UC-043", flow="main", br=["BR-001"], steps=[3])
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

    @pytest.mark.uc("UC-043", flow="main", br=["BR-001"])
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

    @pytest.mark.uc("UC-043", flow="main", br=["BR-001"])
    @pytest.mark.asyncio
    async def test_rng_seed_applies_to_shadow_and_is_dropped_for_real(self, servicer):
        """UC-043 BR-001: the paired CRN ``rng_seed`` seeds a shadow session, and a
        menu-origin (real) start that carries one gets entropy (0) instead."""
        shadow_config = _experiment_config(serials=("a1", "a2"))
        shadow_config.rng_seed = 424242
        real_config = _experiment_config(
            serials=("b1", "b2"),
            origin=game_coordinator_pb2.GAME_ORIGIN_MENU,
            experiment_id="exp_should_not_apply",
        )
        real_config.rng_seed = 424242
        with _NO_THREAD:
            ok_shadow, shadow_gid = await servicer._start_game_from_config(shadow_config, _MockSpan())
            assert ok_shadow is True
            assert servicer.sessions[shadow_gid].game_kind == "shadow"
            assert servicer.sessions[shadow_gid].rng_seed == 424242
            # The real start preempts the shadow game (UC-043 A2); check it alone.
            ok_real, real_gid = await servicer._start_game_from_config(real_config, _MockSpan())
        assert ok_real is True
        assert servicer.sessions[real_gid].game_kind == "primary"
        assert servicer.sessions[real_gid].rng_seed == 0

    @pytest.mark.uc("UC-040", flow="A30", br=["BR-019"])
    @pytest.mark.uc("UC-046", flow="main", br=["BR-018"])
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "origin", [game_coordinator_pb2.GAME_ORIGIN_AGENT, game_coordinator_pb2.GAME_ORIGIN_UNSPECIFIED]
    )
    async def test_pattern_trigger_policy_applies_to_shadow_and_is_dropped_for_real(self, servicer, origin):
        """UC-040 BR-019 (#2096): ``pattern_trigger_policy`` binds only to a shadow session;
        a menu-origin (real) start that carries it gets False, so it never changes a real game."""
        shadow_config = _experiment_config(serials=("a1", "a2"), origin=origin)
        shadow_config.pattern_trigger_policy = True
        real_config = _experiment_config(serials=("b1", "b2"), origin=game_coordinator_pb2.GAME_ORIGIN_MENU)
        real_config.pattern_trigger_policy = True
        with _NO_THREAD:
            ok_shadow, shadow_gid = await servicer._start_game_from_config(shadow_config, _MockSpan())
            assert ok_shadow is True
            assert servicer.sessions[shadow_gid].game_kind == "shadow"
            assert servicer.sessions[shadow_gid].pattern_trigger_policy is True
            ok_real, real_gid = await servicer._start_game_from_config(real_config, _MockSpan())
        assert ok_real is True
        assert servicer.sessions[real_gid].game_kind == "primary"
        assert servicer.sessions[real_gid].pattern_trigger_policy is False

    @pytest.mark.asyncio
    async def test_shadow_experiment_threads_parent_span_context(self, servicer):
        """#1182: the inbound (experiment-traceparent) SpanContext is threaded onto
        the shadow+experiment session as experiment_parent_span_context so the game
        span can be created as a CHILD of the experiment span. Uses a span with a
        VALID (non-zero) trace_id, mirroring an injected experiment traceparent."""
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(
                _experiment_config(serials=("a1", "a2"), experiment_id="exp_abc123", arm="experimental"),
                _ValidParentSpan(),
            )
        assert ok is True
        session = servicer.sessions[gid]
        # The parent context is captured from the inbound span's SpanContext.
        assert session.experiment_parent_span_context is not None
        assert session.experiment_parent_span_context.trace_id == _VALID_PARENT_TRACE_ID

    @pytest.mark.uc("UC-035", flow="main", steps=[5])
    @pytest.mark.asyncio
    async def test_shadow_experiment_target_is_the_injected_span_not_the_server_span(self, servicer, monkeypatch):
        """#2223: with grpc_rpc_spans on, the current span is the interceptor's SERVER
        span, a child of the injected agent.experiment context. The session's
        experiment target is the injected context, while the stream Link keeps the
        server span."""
        from opentelemetry import trace as ot_trace
        from opentelemetry.sdk.trace import TracerProvider

        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)  # a recording SDK span, like the interceptor's

        injected = ot_trace.SpanContext(
            trace_id=_VALID_PARENT_TRACE_ID,
            span_id=0x1122334455667788,
            is_remote=True,
            trace_flags=ot_trace.TraceFlags(ot_trace.TraceFlags.SAMPLED),
        )
        server_span = (
            TracerProvider()
            .get_tracer(__name__)
            .start_span(
                "GameCoordinatorService/StreamGameEvents",
                context=ot_trace.set_span_in_context(ot_trace.NonRecordingSpan(injected)),
                kind=ot_trace.SpanKind.SERVER,
            )
        )
        with _NO_THREAD:
            ok, gid = await servicer._start_game_from_config(
                _experiment_config(serials=("a1", "a2"), experiment_id="exp_abc123", arm="experimental"),
                server_span,
            )
        server_span.end()
        assert ok is True
        session = servicer.sessions[gid]
        assert session.experiment_parent_span_context == injected
        assert session.stream_span_context == server_span.get_span_context()
        assert session.stream_span_context.span_id != injected.span_id

    def test_injected_parent_span_context_none_for_a_server_span_without_traceparent(self, monkeypatch):
        """#2223: a SERVER span with no remote parent (nothing injected) names no experiment target."""
        from opentelemetry.sdk.trace import TracerProvider

        from services.game_coordinator.servicer import injected_parent_span_context

        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
        span = TracerProvider().get_tracer(__name__).start_span("GameCoordinatorService/StreamGameEvents")
        assert injected_parent_span_context(span) is None
        assert injected_parent_span_context(None) is None

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


class _MockSpanContext:
    """trace_id=0 (invalid, like an unsampled span) so the servicer attempts no
    #1157 game-trace Link for direct _start_game_from_config callers."""

    trace_id = 0
    span_id = 0


_VALID_PARENT_TRACE_ID = 0x9999AAAABBBBCCCCDDDDEEEEFFFF0000


class _ValidParentSpanContext:
    """A VALID (non-zero) SpanContext standing in for an injected experiment
    traceparent (#1182), so the servicer captures it as the game-span parent."""

    trace_id = _VALID_PARENT_TRACE_ID
    span_id = 0x1122334455667788


class _MockSpan:
    """Minimal span supporting set_attribute + context-manager protocol."""

    def __init__(self):
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        pass

    def get_span_context(self):
        return _MockSpanContext()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _ValidParentSpan(_MockSpan):
    """A _MockSpan whose SpanContext is VALID (non-zero trace_id), simulating an
    inbound experiment traceparent (#1182)."""

    def get_span_context(self):
        return _ValidParentSpanContext()


class _HoldGame:
    """Minimal built game: runs until force_end() or finish() (same event loop)."""

    def __init__(self):
        self._done = asyncio.Event()

    def get_game_name(self):
        return "FFA"

    async def run(self):
        await self._done.wait()

    def force_end(self, reason=""):
        self._done.set()

    finish = force_end


class TestGameModeSeriesLifecycle:
    """game_current_mode (mode, game_kind, game_id) exists while the game runs and
    is gone after force-end, natural end and retire (FR-041, #1336)."""

    @pytest.fixture
    def harness(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        from services.game_coordinator import metrics

        gauge = metrics.current_game_mode
        monkeypatch.setattr(gauge, "_instrument_initialized", True)
        monkeypatch.setattr(gauge, "_values", {})
        monkeypatch.setattr(gauge, "_metrics", {})
        games, tasks = [], []

        def create_game(**_kwargs):
            games.append(_HoldGame())
            return games[-1]

        def start_in_loop(session):
            # Run the real session loop as a task on this loop instead of a thread.
            clients = MagicMock()
            clients.connect = AsyncMock()
            clients.close = AsyncMock()
            clients.is_connected = True
            session.clients = clients
            session.game_running = True
            tasks.append(asyncio.get_running_loop().create_task(session._run_game_loop_async()))

        monkeypatch.setattr(game_session_mod.GameFactory, "create_game", staticmethod(create_game))
        monkeypatch.setattr(game_session_mod.GameSession, "start_thread", start_in_loop)
        return gauge, games, tasks

    @staticmethod
    async def _wait_for(predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("condition not reached")

    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    @pytest.mark.asyncio
    async def test_mode_series_live_then_gone_after_force_end_and_retire(self, harness):
        gauge, _games, tasks = harness
        servicer = GameCoordinatorServicer()
        ok, game_id = await servicer._start_game_from_config(_config(game_name="JoustFFA"), _MockSpan())
        assert ok, game_id
        await self._wait_for(lambda: gauge._values)
        assert {dict(k)["mode"]: dict(k)["game_id"] for k in gauge._values} == {"FFA": game_id}

        resp = await servicer.ForceEndGame(
            game_coordinator_pb2.ForceEndGameRequest(game_id=game_id, reason="test"), MockGrpcContext()
        )
        assert resp.success is True
        await asyncio.gather(*tasks)
        assert game_id not in servicer.sessions  # force-end retires eagerly
        assert gauge._values == {}

    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    @pytest.mark.asyncio
    async def test_mode_series_gone_after_natural_end_before_retire(self, harness):
        gauge, games, tasks = harness
        servicer = GameCoordinatorServicer()
        ok, game_id = await servicer._start_game_from_config(_config(), _MockSpan())
        assert ok, game_id
        await self._wait_for(lambda: gauge._values)

        games[0].finish()
        await asyncio.gather(*tasks)
        # Natural end: still registered (retired lazily on the next start) but
        # the mode series is already gone.
        assert game_id in servicer.sessions
        assert gauge._values == {}

        # Retire (via the next start's sweep) is an idempotent backstop.
        ok2, game_id2 = await servicer._start_game_from_config(_config(serials=("b1", "b2")), _MockSpan())
        assert ok2, game_id2
        assert game_id not in servicer.sessions
        await self._wait_for(lambda: gauge._values)
        assert {dict(k)["game_id"] for k in gauge._values} == {game_id2}
        games[1].finish()
        await asyncio.gather(*tasks)


def _thread_runs_first(session):
    """Stand-in for ``GameSession.start_thread`` where the game thread wins the race (#1783).

    A real thread on its own event loop publishes ``game_starting`` on the
    session bus and is joined before ``start_thread`` returns, so the event is
    published before the servicer gets the loop back.
    """
    thread = threading.Thread(
        target=lambda: asyncio.run(session.event_bus.publish(GameEvent.GAME_STARTING, {"game_id": session.game_id}))
    )
    thread.start()
    thread.join()


class TestHeadlessStartStream:
    """UC-003 BR-003: a start_config stream receives every event of its game (#1783)."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-003", flow="A2", br=["BR-003"])
    async def test_starter_receives_events_the_game_thread_publishes_before_start_returns(self, servicer):
        request = game_coordinator_pb2.StreamEventsRequest(start_config=_shadow_config())
        context = MockGrpcContext()

        with patch.object(game_session_mod.GameSession, "start_thread", _thread_runs_first):
            collected = await _collect_stream_events(servicer, request, context, until=GameEvent.GAME_STARTING)

        types = [e.event_type for e in collected]
        assert types == ["game_start", "game_starting"], types
        game_id = collected[0].data["game_id"]
        assert game_id in servicer.sessions
        assert collected[1].data["game_id"] == game_id

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-003", flow="A2", br=["BR-003"])
    async def test_failed_start_after_admission_yields_error_and_leaves_no_subscriber(self, servicer):
        sessions = []

        def _start_fails(session):
            sessions.append(session)
            raise RuntimeError("thread start failed")

        request = game_coordinator_pb2.StreamEventsRequest(start_config=_shadow_config())
        context = MockGrpcContext()
        with patch.object(game_session_mod.GameSession, "start_thread", _start_fails):
            collected = [event async for event in servicer.StreamGameEvents(request, context)]

        assert [e.event_type for e in collected] == ["game_start_error"]
        assert "thread start failed" in collected[0].data["error"]
        assert len(sessions) == 1
        assert sessions[0].event_bus.subscriber_count == 0

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-003", flow="A2", br=["BR-003"])
    async def test_client_cancel_mid_start_leaves_no_subscriber(self, servicer):
        """A cancel during the flag-service register (after the subscribe) drops the subscriber."""
        register_entered = asyncio.Event()

        async def _register_blocks(_record):
            register_entered.set()
            await asyncio.Event().wait()

        async def _drain(gen):
            async for _event in gen:
                pass

        request = game_coordinator_pb2.StreamEventsRequest(start_config=_shadow_config())
        gen = servicer.StreamGameEvents(request, MockGrpcContext())
        with patch.object(servicer.flag_service, "register", _register_blocks):
            drain_task = asyncio.create_task(_drain(gen))
            async with asyncio.timeout(2.0):
                await register_entered.wait()
            (session,) = servicer.sessions.values()
            assert session.event_bus.subscriber_count == 1
            drain_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain_task

        assert session.event_bus.subscriber_count == 0

    @pytest.mark.asyncio
    async def test_game_id_observer_gets_no_replay_of_earlier_events(self, servicer):
        """An observer attaching by game_id gets events from when it attaches (unchanged)."""
        game_id, session = await _start_running(servicer, _shadow_config())
        request = game_coordinator_pb2.StreamEventsRequest(game_id=game_id)
        context = MockGrpcContext()

        async def _publish():
            await session.event_bus.publish("player_death", {"serial": "p1"})

        collected = await _collect_stream_events(servicer, request, context, until="player_death", after=_publish)

        assert [e.event_type for e in collected] == ["player_death"]


class TestFailedStartRetiresSession:
    """UC-010 A7 / BR-007: a start that fails or is cancelled after admission
    retires its session like a normal end (#1791), so it never holds the primary
    slot, its controllers or a shadow-cap slot."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @staticmethod
    def _assert_retired(servicer, game_id):
        assert game_id not in servicer.sessions
        assert servicer._primary_game_id is None
        assert servicer.primary_event_bus.current_game_id == ""
        # Its controllers are no longer held by any live game.
        assert servicer._flag_service_live_games() == []

    @staticmethod
    async def _start_via_stream(servicer, config):
        request = game_coordinator_pb2.StreamEventsRequest(start_config=config)
        return [event async for event in servicer.StreamGameEvents(request, MockGrpcContext())]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A7", br=["BR-007"])
    async def test_start_thread_failure_retires_primary_and_next_start_succeeds(self, servicer):
        admitted = []

        def _start_fails(session):
            admitted.append(session.game_id)
            raise RuntimeError("thread start failed")

        with (
            patch.object(game_session_mod.GameSession, "start_thread", _start_fails),
            patch.object(servicer.flag_service, "end") as flag_end,
        ):
            collected = await self._start_via_stream(servicer, _config())

        assert [e.event_type for e in collected] == ["game_start_error"]
        (game_id,) = admitted
        self._assert_retired(servicer, game_id)
        flag_end.assert_called_once_with(game_id)

        with _NO_THREAD:
            ok, next_id = await servicer._start_game_from_config(_config(), _MockSpan())
        assert ok is True, next_id
        assert servicer._primary_game_id == next_id

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A7", br=["BR-007"])
    async def test_start_thread_failure_after_game_start_publishes_game_start_error_on_primary_bus(self, servicer):
        """A primary-bus observer saw game_start (Running); the failed thread start
        must answer it with game_start_error so it leaves Running."""
        observer = await servicer.primary_event_bus.subscribe("observer")

        def _start_fails(_session):
            raise RuntimeError("thread start failed")

        with patch.object(game_session_mod.GameSession, "start_thread", _start_fails):
            ok, error = await servicer._start_game_from_config(_config(), _MockSpan())

        assert ok is False
        assert "thread start failed" in error
        seen = []
        while not observer.empty():
            seen.append(observer.get_nowait())
        assert [e.event_type for e in seen] == ["game_start", "game_start_error"]
        assert "thread start failed" in seen[1].data["error"]
        assert seen[1].data["game_id"] == seen[0].data["game_id"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A7", br=["BR-007"])
    @patch("services.game_coordinator.servicer.metrics")
    async def test_failed_thread_start_is_not_counted_in_games_started_total(self, mock_metrics, servicer, caplog):
        def _start_fails(_session):
            raise RuntimeError("thread start failed")

        with (
            caplog.at_level(logging.WARNING, logger="services.game_coordinator.servicer"),
            patch.object(game_session_mod.GameSession, "start_thread", _start_fails),
        ):
            ok, _ = await servicer._start_game_from_config(_config(), _MockSpan())

        assert ok is False
        mock_metrics.games_started_total.labels.return_value.inc.assert_not_called()
        assert any("after admission (reason=error)" in r.getMessage() for r in caplog.records)

        with _NO_THREAD:
            ok, _ = await servicer._start_game_from_config(_config(), _MockSpan())
        assert ok is True
        mock_metrics.games_started_total.labels.return_value.inc.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A7", br=["BR-007"])
    async def test_game_start_publish_failure_retires_primary_and_next_start_succeeds(self, servicer):
        real_publish = servicer.primary_event_bus.publish
        admitted = []

        async def _publish_fails(event_type, data):
            if event_type == GameEvent.GAME_START:
                admitted.append(data["game_id"])
                raise RuntimeError("publish failed")
            return await real_publish(event_type, data)

        with _NO_THREAD, patch.object(servicer.primary_event_bus, "publish", _publish_fails):
            collected = await self._start_via_stream(servicer, _config())

        assert [e.event_type for e in collected] == ["game_start_error"]
        assert "publish failed" in collected[0].data["error"]
        (game_id,) = admitted
        self._assert_retired(servicer, game_id)

        with _NO_THREAD:
            ok, next_id = await servicer._start_game_from_config(_config(), _MockSpan())
        assert ok is True, next_id

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A7", br=["BR-007"])
    async def test_cancel_during_register_retires_primary_and_propagates(self, servicer, caplog):
        register_entered = asyncio.Event()

        async def _register_blocks(_record):
            register_entered.set()
            await asyncio.Event().wait()

        with (
            _NO_THREAD,
            patch.object(servicer.flag_service, "register", _register_blocks),
            patch.object(servicer.flag_service, "end") as flag_end,
        ):
            start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
            async with asyncio.timeout(2.0):
                await register_entered.wait()
            game_id = servicer._primary_game_id
            assert game_id in servicer.sessions
            start.cancel()
            with (
                caplog.at_level(logging.WARNING, logger="services.game_coordinator.servicer"),
                pytest.raises(asyncio.CancelledError),
            ):
                await start
        assert any("after admission (reason=cancelled)" in r.getMessage() for r in caplog.records)

        self._assert_retired(servicer, game_id)
        flag_end.assert_called_once_with(game_id)

        with _NO_THREAD:
            ok, next_id = await servicer._start_game_from_config(_config(), _MockSpan())
        assert ok is True, next_id

    @staticmethod
    def _patch_post_admission_preempt(servicer, behaviour):
        """Run ``behaviour`` in the real start's preemption pass that follows its
        admission (#1404), i.e. the pass that sees the primary slot taken."""
        real_preempt = servicer._preempt_shadow_sessions

        async def preempt():
            if servicer._primary_game_id is not None:
                return await behaviour()
            return await real_preempt()

        return patch.object(servicer, "_preempt_shadow_sessions", preempt)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A7", br=["BR-007"])
    async def test_cancel_during_post_admission_preemption_retires_primary(self, servicer):
        """#1813: a client cancel while the real start preempts shadows after its
        admission (force_end waits up to 2 s) retires the session; the next real
        start is admitted."""
        preempt_entered = asyncio.Event()

        async def _preempt_blocks():
            preempt_entered.set()
            await asyncio.Event().wait()

        with _NO_THREAD, self._patch_post_admission_preempt(servicer, _preempt_blocks):
            start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
            async with asyncio.timeout(2.0):
                await preempt_entered.wait()
            game_id = servicer._primary_game_id
            assert game_id in servicer.sessions
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start

        self._assert_retired(servicer, game_id)
        with _NO_THREAD:
            ok, next_id = await servicer._start_game_from_config(_config(), _MockSpan())
        assert ok is True, next_id
        assert servicer._primary_game_id == next_id

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A7", br=["BR-007"])
    async def test_error_during_post_admission_preemption_retires_primary(self, servicer):
        """#1813: an error raised by the real start's post-admission preemption pass
        itself (here its shadow snapshot; a shadow's force_end error is caught per
        shadow since #1816) retires the session; the next real start is admitted."""
        admitted = []
        real_snapshot = servicer._live_shadow_sessions

        def _snapshot_fails_after_admission():
            if servicer._primary_game_id is None:
                return real_snapshot()
            admitted.append(servicer._primary_game_id)
            raise RuntimeError("shadow snapshot failed")

        with _NO_THREAD, patch.object(servicer, "_live_shadow_sessions", _snapshot_fails_after_admission):
            ok, error = await servicer._start_game_from_config(_config(), _MockSpan())

        assert ok is False
        assert "shadow snapshot failed" in error
        (game_id,) = admitted
        self._assert_retired(servicer, game_id)
        with _NO_THREAD:
            ok, next_id = await servicer._start_game_from_config(_config(), _MockSpan())
        assert ok is True, next_id
        assert servicer._primary_game_id == next_id

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A7", br=["BR-007"])
    async def test_failed_shadow_start_frees_its_shadow_cap_slot(self, servicer):
        def _start_fails(_session):
            raise RuntimeError("thread start failed")

        with _caps(concurrent=8, shadow=1):
            with patch.object(game_session_mod.GameSession, "start_thread", _start_fails):
                ok, error = await servicer._start_game_from_config(_shadow_config(), _MockSpan())
            assert ok is False
            assert "thread start failed" in error
            assert servicer.sessions == {}

            with _NO_THREAD:
                ok, game_id = await servicer._start_game_from_config(_shadow_config(), _MockSpan())
        assert ok is True, game_id


class _NaturalEndOnlyGame(_HoldGame):
    """A built game that ignores force_end(): only finish() (its natural end) stops it."""

    def force_end(self, reason=""):
        pass

    def finish(self):
        self._done.set()


class TestNaturalEndReportsFlagServiceEnd:
    """A game that ends on its own is ended in the flag service right away, exactly
    once, not at the next start's sweep or the next 15 s reconcile (UC-053 step 7,
    FR-135, #1847)."""

    @pytest.fixture
    def harness(self, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock

        games, tasks = [], []
        game_class = {"cls": _HoldGame}

        def create_game(**_kwargs):
            games.append(game_class["cls"]())
            return games[-1]

        def start_in_loop(session):
            # The real session loop (with its end-of-game finally) as a task on
            # this loop: a deterministic stand-in for the game thread.
            clients = MagicMock()
            clients.connect = AsyncMock()
            clients.close = AsyncMock()
            clients.is_connected = True
            session.clients = clients
            session.game_running = True
            tasks.append(asyncio.get_running_loop().create_task(session._run_game_loop_async()))

        monkeypatch.setattr(game_session_mod.GameFactory, "create_game", staticmethod(create_game))
        monkeypatch.setattr(game_session_mod.GameSession, "start_thread", start_in_loop)
        return games, tasks, game_class

    @staticmethod
    async def _wait_for(predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("condition not reached")

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-053", flow="main", steps=[7])
    async def test_natural_end_ends_the_game_in_the_flag_service_once(self, harness):
        games, tasks, _cls = harness
        servicer = GameCoordinatorServicer()
        with patch.object(servicer.flag_service, "end") as flag_end:
            ok, game_id = await servicer._start_game_from_config(_config(), _MockSpan())
            assert ok, game_id
            await self._wait_for(lambda: games)

            games[0].finish()
            await asyncio.gather(*tasks)

            # Reported at the end, before any later start or reconcile.
            assert flag_end.call_args_list == [call(game_id)]
            # The session stays registered: GetGameState keeps answering ENDED.
            assert servicer.sessions[game_id].game_state == game_coordinator_pb2.GameState.ENDED
            assert servicer._flag_service_live_games() == []

            # The next start's ENDED sweep retires it without a second end.
            ok2, game_id2 = await servicer._start_game_from_config(_config(serials=("b1", "b2")), _MockSpan())
            assert ok2, game_id2
            assert game_id not in servicer.sessions
            assert [c for c in flag_end.call_args_list if c.args == (game_id,)] == [call(game_id)]

            await self._wait_for(lambda: len(games) == 2)
            games[1].finish()
            await asyncio.gather(*tasks)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-053", flow="main", steps=[7])
    @pytest.mark.uc("UC-043", flow="A2")
    async def test_natural_end_during_a_preemption_ends_the_game_once(self, harness):
        """The shadow's game ends on its own while its preemption joins the game
        thread; the preemption then retires it. One flag-service end in total."""
        games, tasks, game_class = harness
        game_class["cls"] = _NaturalEndOnlyGame
        servicer = GameCoordinatorServicer()
        with _caps(concurrent=8, shadow=1), patch.object(servicer.flag_service, "end") as flag_end:
            ok, gid_s = await servicer._start_game_from_config(_shadow_config(serials=("s1", "s2")), _MockSpan())
            assert ok, gid_s
            shadow = servicer.sessions[gid_s]
            await self._wait_for(lambda: games)
            await shadow.event_bus.publish(GameEvent.GAME_STARTED, {"game_id": gid_s})
            thread = _give_blocking_thread(shadow)

            with _NO_THREAD:
                start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
                async with asyncio.timeout(2.0):
                    await thread.entered.wait()

                # Natural end while the preemption's force end joins the thread.
                games[0].finish()
                await asyncio.gather(*tasks)
                assert flag_end.call_args_list == [call(gid_s)]

                thread.release.set()
                async with asyncio.timeout(2.0):
                    ok_real, gid_real = await start
            assert ok_real, gid_real
            assert gid_s not in servicer.sessions
            assert [c for c in flag_end.call_args_list if c.args == (gid_s,)] == [call(gid_s)]
        await asyncio.sleep(0)  # the preemption task's done callback runs
        assert not servicer._session_endings


class TestForceEndOnce:
    """UC-027 steps 3-4, A4, BR-003 (#1827): a force end completes even when its
    caller is cancelled, and a session is ended once, whichever of ForceEndGame,
    ``end_game`` and a real-start preemption reaches it first."""

    @pytest.fixture
    def servicer(self):
        servicer = GameCoordinatorServicer()
        yield servicer
        # No session end task outlives its test.
        assert not servicer._session_endings

    @pytest.fixture(autouse=True)
    def policy_allow(self):
        with patch("services.game_coordinator.servicer._shadow_policy", lambda: "allow"):
            yield

    _drain = staticmethod(TestShadowGameGovernance._drain)
    _record_retires = staticmethod(TestShadowGameGovernance._record_retires)

    @staticmethod
    def _count_retires(servicer):
        retires = []
        real_retire = servicer._retire_session

        def retire(gid):
            retires.append(gid)
            real_retire(gid)

        servicer._retire_session = retire
        return retires

    @staticmethod
    def _mode(session):
        game = create_autospec(BaseGameMode, instance=True)
        session.current_game = game
        return game

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[3, 4])
    async def test_cancelled_force_end_still_ends_and_retires(self, servicer):
        """ForceEndGame (primary) cancelled during the join: the end still
        publishes game_force_ended once and retires the session."""
        gid, session = await _start_running(servicer, _config())
        thread = _give_blocking_thread(session)
        q = await session.event_bus.subscribe("watch")
        retired = self._record_retires(servicer, gid)

        call_task = asyncio.create_task(
            servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="operator"), MockGrpcContext())
        )
        async with asyncio.timeout(2.0):
            await thread.entered.wait()
        call_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task
        assert gid in servicer.sessions

        thread.release.set()
        async with asyncio.timeout(2.0):
            await retired.wait()
        await asyncio.sleep(0)  # the end task's done callback runs (leak guard)

        assert [e.data["reason"] for e in self._drain(q)] == ["operator"]
        assert gid not in servicer.sessions
        assert servicer._primary_game_id is None
        assert servicer._flag_service_live_games() == []
        with _NO_THREAD:
            ok, gid_next = await servicer._start_game_from_config(_config(serials=("p3", "p4")), _MockSpan())
        assert ok is True, gid_next
        assert servicer._primary_game_id == gid_next

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="A3")
    async def test_cancelled_end_game_still_ends_and_retires_the_shadow(self, servicer):
        """The end_game path (``_force_end_session`` with a shadow id) cancelled
        during the join still ends and retires that shadow."""
        gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        thread = _give_blocking_thread(shadow)
        q = await shadow.event_bus.subscribe("watch")
        retired = self._record_retires(servicer, gid_s)

        end_task = asyncio.create_task(servicer._force_end_session(gid_s, "agent_intervention"))
        async with asyncio.timeout(2.0):
            await thread.entered.wait()
        end_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await end_task

        thread.release.set()
        async with asyncio.timeout(2.0):
            await retired.wait()
        await asyncio.sleep(0)

        assert [e.data["reason"] for e in self._drain(q)] == ["agent_intervention"]
        assert gid_s not in servicer.sessions
        assert servicer._flag_service_live_games() == []

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="A4", br=["BR-003"])
    @pytest.mark.uc("UC-040", flow="A18")
    @pytest.mark.parametrize("second", ["force_end_game", "end_game"])
    async def test_concurrent_force_ends_join_one_end(self, servicer, second, caplog):
        """A second force end (ForceEndGame or the agent's end_game) during the
        join waits for the first: one join, one mode force end, one
        game_force_ended with the first reason, one flag-service end."""
        gid, session = await _start_running(servicer, _config())
        thread = _give_blocking_thread(session)
        game = self._mode(session)
        q = await session.event_bus.subscribe("watch")

        with (
            patch.object(servicer.flag_service, "end") as flag_end,
            caplog.at_level(logging.INFO),
        ):
            first = asyncio.create_task(
                servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="operator"), MockGrpcContext())
            )
            async with asyncio.timeout(2.0):
                await thread.entered.wait()

            if second == "force_end_game":
                second_call = servicer.ForceEndGame(
                    game_coordinator_pb2.ForceEndGameRequest(game_id=gid, reason="retry"), MockGrpcContext()
                )
            else:
                ctx = InterventionContext(
                    spec=None, value="", payload="", target_serial=None, game=None, objective="", game_id=gid
                )
                second_call = servicer.intervention_manager._handle_end_game(ctx)
            second_task = asyncio.create_task(second_call)
            await asyncio.sleep(0)  # the second caller reaches the end under way

            thread.release.set()
            async with asyncio.timeout(2.0):
                first_resp = await first
                second_result = await second_task
            await asyncio.sleep(0)

        assert first_resp.success is True, first_resp.error
        if second == "force_end_game":
            assert second_result.success is True, second_result.error
        else:
            assert any(
                f"end_game: game {gid} force-ended via agent intervention" in r.getMessage() for r in caplog.records
            )
        second_reason = "retry" if second == "force_end_game" else "agent_intervention"
        assert any(
            r.levelno == logging.INFO and f"reason {second_reason!r} joins it" in r.getMessage() for r in caplog.records
        )
        assert thread.joins == 1
        game.force_end.assert_called_once_with("operator")
        assert [e.data["reason"] for e in self._drain(q)] == ["operator"]
        assert flag_end.call_args_list == [call(gid)]
        assert gid not in servicer.sessions

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="A4", br=["BR-003"])
    @pytest.mark.uc("UC-043", flow="A2")
    async def test_force_end_waits_for_a_preemption_under_way(self, servicer):
        """A real start preempts the shadow; ForceEndGame for the shadow during
        the join waits for that preemption: one end, reason
        preempted_by_real_game, counted once."""
        with _caps(concurrent=8, shadow=1), patch.object(metrics, "shadow_games_preempted_total") as preempted:
            gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
            thread = _give_blocking_thread(shadow)
            q = await shadow.event_bus.subscribe("watch")

            with _NO_THREAD:
                start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
                async with asyncio.timeout(2.0):
                    await thread.entered.wait()
                force = asyncio.create_task(
                    servicer.ForceEndGame(
                        game_coordinator_pb2.ForceEndGameRequest(game_id=gid_s, reason="operator"), MockGrpcContext()
                    )
                )
                await asyncio.sleep(0)
                thread.release.set()
                async with asyncio.timeout(2.0):
                    ok, gid_real = await start
                    resp = await force
            await asyncio.sleep(0)

        assert ok is True, gid_real
        assert resp.success is True, resp.error
        assert thread.joins == 1
        assert [e.data["reason"] for e in self._drain(q)] == ["preempted_by_real_game"]
        preempted.inc.assert_called_once_with()
        assert gid_s not in servicer.sessions

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-043", flow="A2")
    @pytest.mark.uc("UC-027", flow="A4", br=["BR-003"])
    async def test_preemption_waits_for_a_force_end_under_way(self, servicer):
        """ForceEndGame of the shadow first, then a real start: the preemption
        waits for that end, which keeps its reason and is not counted as a
        preemption; the shadow is retired once and the real start admitted."""
        with _caps(concurrent=8, shadow=1), patch.object(metrics, "shadow_games_preempted_total") as preempted:
            gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
            thread = _give_blocking_thread(shadow)
            q = await shadow.event_bus.subscribe("watch")
            retires = self._count_retires(servicer)

            force = asyncio.create_task(
                servicer.ForceEndGame(
                    game_coordinator_pb2.ForceEndGameRequest(game_id=gid_s, reason="operator"), MockGrpcContext()
                )
            )
            async with asyncio.timeout(2.0):
                await thread.entered.wait()

            preempt_entered = asyncio.Event()
            real_preempt = servicer._preempt_shadow_sessions

            async def preempt():
                preempt_entered.set()
                return await real_preempt()

            servicer._preempt_shadow_sessions = preempt
            with _NO_THREAD, patch.object(servicer.flag_service, "end") as flag_end:
                start = asyncio.create_task(servicer._start_game_from_config(_config(), _MockSpan()))
                async with asyncio.timeout(2.0):
                    await preempt_entered.wait()
                thread.release.set()
                async with asyncio.timeout(2.0):
                    resp = await force
                    ok, gid_real = await start
            await asyncio.sleep(0)

        assert resp.success is True, resp.error
        assert ok is True, gid_real
        assert servicer._primary_game_id == gid_real
        assert thread.joins == 1
        assert [e.data["reason"] for e in self._drain(q)] == ["operator"]
        preempted.inc.assert_not_called()
        assert retires.count(gid_s) == 1
        assert [c for c in flag_end.call_args_list if c.args == (gid_s,)] == [call(gid_s)]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="A2")
    async def test_no_op_force_end_logs_no_error(self, servicer, caplog):
        """A ForceEndGame or end_game for a registered session whose game already
        ended answers No game in progress and logs no error."""
        gid, session = await _start_running(servicer, _config())
        with session._state_lock:
            session.game_state = game_coordinator_pb2.GameState.ENDED

        with caplog.at_level(logging.INFO):
            resp = await servicer.ForceEndGame(
                game_coordinator_pb2.ForceEndGameRequest(reason="operator"), MockGrpcContext()
            )
            ctx = InterventionContext(
                spec=None, value="", payload="", target_serial=None, game=None, objective="", game_id=gid
            )
            await servicer.intervention_manager._handle_end_game(ctx)
            await asyncio.sleep(0)

        assert resp.success is False
        assert resp.error == "No game in progress"
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
        assert any(f"end_game: no-op for game {gid}" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-027", flow="main", steps=[4])
    async def test_shutdown_waits_for_a_pending_force_end(self, servicer):
        """A ForceEndGame cancelled during the join still retires its session
        before shutdown flushes and stops the flag-service lifecycle (#1858)."""
        gid, session = await _start_running(servicer, _config())
        thread = _give_blocking_thread(session)
        order = []
        real_retire = servicer._retire_session

        def retire(game_id):
            order.append(f"retire:{game_id}")
            real_retire(game_id)

        servicer._retire_session = retire

        async def flush(*_args, **_kwargs):
            order.append("flush")

        async def stop():
            order.append("stop")

        call_task = asyncio.create_task(
            servicer.ForceEndGame(game_coordinator_pb2.ForceEndGameRequest(reason="operator"), MockGrpcContext())
        )
        async with asyncio.timeout(2.0):
            await thread.entered.wait()
        call_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task

        with (
            patch.object(session, "join_thread", AsyncMock()),
            patch.object(servicer.flag_service, "flush", flush),
            patch.object(servicer.flag_service, "stop", stop),
        ):
            shutdown = asyncio.create_task(servicer.shutdown())
            await asyncio.sleep(0)  # shutdown runs up to its first await
            thread.release.set()
            async with asyncio.timeout(5.0):
                await shutdown
        await asyncio.sleep(0)

        assert order == [f"retire:{gid}", "flush", "stop"]

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-053", flow="main", steps=[7])
    @pytest.mark.uc("UC-027", flow="A4", br=["BR-003"])
    async def test_natural_end_during_force_end_join_ends_and_cleans_up_once(self, servicer):
        """Order: ForceEndGame joins the shadow's thread; the game ends on its
        own (ENDED + the #1848 end hook); the next start's ENDED sweep retires
        the session; then the join returns. The flag-service end and the gauge
        cleanup happen once for the session, and game_force_ended once."""
        gid_s, shadow = await _start_running(servicer, _shadow_config(serials=("s1", "s2")))
        thread = _give_blocking_thread(shadow)
        q = await shadow.event_bus.subscribe("watch")

        with (
            patch.object(servicer.flag_service, "end") as flag_end,
            patch.object(metrics, "clear_game_state_gauges") as clear_state,
        ):
            force = asyncio.create_task(
                servicer.ForceEndGame(
                    game_coordinator_pb2.ForceEndGameRequest(game_id=gid_s, reason="operator"), MockGrpcContext()
                )
            )
            async with asyncio.timeout(2.0):
                await thread.entered.wait()

            # Natural end on the game thread: ENDED, then the end hook.
            with shadow._state_lock:
                shadow.game_state = game_coordinator_pb2.GameState.ENDED
            servicer._on_session_game_ended(shadow)
            # The next start's sweep retires the ENDED session.
            with _NO_THREAD:
                ok, gid = await servicer._start_game_from_config(_shadow_config(serials=("s3", "s4")), _MockSpan())
            assert ok is True, gid
            assert gid_s not in servicer.sessions

            thread.release.set()
            async with asyncio.timeout(2.0):
                resp = await force
            await asyncio.sleep(0)

        assert resp.success is True, resp.error
        assert [c for c in flag_end.call_args_list if c.args == (gid_s,)] == [call(gid_s)]
        assert [c for c in clear_state.call_args_list if c.args == ("shadow", gid_s)] == [call("shadow", gid_s)]
        assert [e.data["reason"] for e in self._drain(q)] == ["operator"]
        assert gid in servicer.sessions


# Per-mode minimum players (UC-010 BR-001, #1862). The values are pinned here, not
# read from lib/types.py, so a changed minimum shows up as a test failure.
_MODE_MINIMUMS = [
    ("JoustFFA", 2),
    ("JoustTeams", 3),
    ("JoustRandomTeams", 3),
    ("Traitor", 4),
    ("Werewolf", 3),
    ("Zombies", 4),
    ("Swapper", 3),
    ("FightClub", 2),
    ("Tournament", 3),
    ("NonStop", 2),
]


class TestModeMinimumPlayers:
    """UC-010 BR-001: the coordinator refuses a start below the mode's minimum, for every origin."""

    @pytest.fixture
    def servicer(self):
        return GameCoordinatorServicer()

    @staticmethod
    def _serials(count):
        return tuple(f"p{i}" for i in range(1, count + 1))

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", br=["BR-001"])
    @pytest.mark.uc("UC-021", cond=["Pre-1"])
    @pytest.mark.uc("UC-022", cond=["Pre-1"])
    @pytest.mark.uc("UC-023", cond=["Pre-1"])
    @pytest.mark.uc("UC-024", cond=["Pre-1"])
    @pytest.mark.uc("UC-025", cond=["Pre-1"])
    @pytest.mark.parametrize("make_config", [_config, _shadow_config], ids=["real", "shadow"])
    @pytest.mark.parametrize(("game_name", "minimum"), _MODE_MINIMUMS)
    async def test_below_minimum_is_refused_and_registers_nothing(self, servicer, make_config, game_name, minimum):
        config = make_config(game_name=game_name, serials=self._serials(minimum - 1))

        with _NO_THREAD:
            success, message = await servicer._start_game_from_config(config, _MockSpan())

        assert success is False
        if minimum == 2:
            assert message == "Need at least 2 players"
        else:
            assert message == f"{game_name} needs at least {minimum} players"
        assert servicer.sessions == {}
        assert servicer.game_id is None

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="main", steps=[5], br=["BR-001"])
    @pytest.mark.uc("UC-021", cond=["Pre-1"])
    @pytest.mark.uc("UC-022", cond=["Pre-1"])
    @pytest.mark.uc("UC-023", cond=["Pre-1"])
    @pytest.mark.uc("UC-024", cond=["Pre-1"])
    @pytest.mark.uc("UC-025", cond=["Pre-1"])
    @pytest.mark.parametrize("make_config", [_config, _shadow_config], ids=["real", "shadow"])
    @pytest.mark.parametrize(("game_name", "minimum"), _MODE_MINIMUMS)
    async def test_at_minimum_starts(self, servicer, make_config, game_name, minimum):
        config = make_config(game_name=game_name, serials=self._serials(minimum))

        with _NO_THREAD:
            success, game_id = await servicer._start_game_from_config(config, _MockSpan())

        assert success is True, game_id
        assert game_id in servicer.sessions

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", br=["BR-001"])
    async def test_minimum_error_takes_precedence_over_game_in_progress(self, servicer):
        """Error order: a sub-minimum start while a game runs answers the minimum error, not 'already in progress'."""
        with _NO_THREAD:
            ok, _ = await servicer._start_game_from_config(_config(), _MockSpan())
            assert ok is True

            success, message = await servicer._start_game_from_config(
                _config(game_name="Traitor", serials=self._serials(3)), _MockSpan()
            )

        assert success is False
        assert message == "Traitor needs at least 4 players"

    @pytest.mark.asyncio
    @pytest.mark.uc("UC-010", flow="A3", br=["BR-001"])
    @pytest.mark.uc("UC-022", cond=["Pre-1"])
    async def test_stream_start_below_minimum_answers_game_start_error(self, servicer):
        """A direct StreamGameEvents(start_config) start cannot bypass the menu's check."""
        request = game_coordinator_pb2.StreamEventsRequest(
            start_config=_config(game_name="Traitor", serials=self._serials(3))
        )

        with _NO_THREAD:
            collected = await _collect_stream_events(servicer, request, MockGrpcContext(), until="game_start_error")

        assert [e.event_type for e in collected] == ["game_start_error"]
        assert collected[0].data["error"] == "Traitor needs at least 4 players"
        assert servicer.sessions == {}
