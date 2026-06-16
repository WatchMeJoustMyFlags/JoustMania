"""
Unit tests for GameSession (#775).

The game-loop lifecycle (connect clients -> create game -> run -> cleanup) and
its metric side effects moved from the servicer onto GameSession in the
multi-session refactor. These tests pin that loop behavior, mirroring the
former TestRunGameLoopAsync suite but operating on a GameSession instance, plus
new coverage for the primary/shadow distinction.
"""

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from proto import game_coordinator_pb2
from services.game_coordinator.game_session import GAME_KIND_PRIMARY, GAME_KIND_SHADOW, GameSession


class MockSpanContext:
    """Mock SpanContext exposing trace_id + span_id (#1133/#1157 gauge reads both)."""

    def __init__(self, trace_id, span_id=0x051581BF3CB55C13):
        self.trace_id = trace_id
        self.span_id = span_id


class MockSpan:
    """Mock OpenTelemetry span supporting set_attribute + context manager."""

    def __init__(self, trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736, span_id=0x051581BF3CB55C13):
        self.attributes = {}
        # Non-zero trace_id by default so the #1133 correlation gauge fires; a test
        # can pass trace_id=0 to exercise the unsampled-span fallback (no gauge).
        self._span_context = MockSpanContext(trace_id, span_id)

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        pass

    def get_span_context(self):
        return self._span_context

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _make_session(game_kind=GAME_KIND_PRIMARY, experiment_id="", arm=""):
    """Build a GameSession with mocked clients + event bus for loop testing."""
    event_bus = MagicMock()
    event_bus.publish = AsyncMock()

    session = GameSession(
        game_id="game_abc123",
        game_name="FFA",
        players=[
            game_coordinator_pb2.Player(serial="p1"),
            game_coordinator_pb2.Player(serial="p2"),
        ],
        game_config=game_coordinator_pb2.StartGameConfig(sensitivity=2),
        event_bus=event_bus,
        game_kind=game_kind,
        stream_span_context=None,
        experiment_id=experiment_id,
        arm=arm,
    )

    mock_clients = MagicMock()
    mock_clients.connect = AsyncMock()
    mock_clients.close = AsyncMock()
    mock_clients.is_connected = True
    mock_clients.controller_manager = MagicMock()
    mock_clients.audio = MagicMock()
    session.clients = mock_clients

    return session


def _tracer_mock(trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736, span_id=0x051581BF3CB55C13):
    mock_span = MockSpan(trace_id=trace_id, span_id=span_id)
    mock_tracer = MagicMock()
    mock_tracer.start_as_current_span.return_value.__enter__ = MagicMock(return_value=mock_span)
    mock_tracer.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)
    return mock_tracer


class TestGameSessionLoop:
    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_connects_clients(self, mock_tracer_mod, mock_factory):
        session = _make_session()
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        session.clients.connect.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_clients_not_connected_publishes_error(self, mock_tracer_mod, mock_factory):
        session = _make_session()
        session.clients.is_connected = False
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span

        await session._run_game_loop_async()

        error_calls = [c for c in session.event_bus.publish.call_args_list if c[0][0] == "game_error"]
        assert len(error_calls) >= 1
        assert "not initialized" in error_calls[0][0][1]["error"]
        mock_factory.create_game.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_unknown_mode_publishes_error(self, mock_tracer_mod, mock_factory):
        session = _make_session()
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_factory.create_game.side_effect = ValueError("Unknown game mode: 'BadGame'")

        await session._run_game_loop_async()

        error_calls = [c for c in session.event_bus.publish.call_args_list if c[0][0] == "game_error"]
        assert len(error_calls) >= 1
        assert "Unknown game mode" in error_calls[0][0][1]["error"]

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_runs_game_to_completion(self, mock_tracer_mod, mock_factory):
        session = _make_session()
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        session.clients.connect.assert_awaited_once()
        mock_factory.create_game.assert_called_once()
        mock_game.run.assert_awaited_once()
        session.clients.close.assert_awaited()
        assert session.game_running is False
        assert session.current_game is None

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_exception_publishes_error(self, mock_tracer_mod, mock_factory):
        session = _make_session()
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock(side_effect=RuntimeError("stream disconnected"))
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        error_calls = [c for c in session.event_bus.publish.call_args_list if c[0][0] == "game_error"]
        assert len(error_calls) >= 1
        assert "stream disconnected" in error_calls[0][0][1]["error"]

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_always_closes_clients(self, mock_tracer_mod, mock_factory):
        session = _make_session()
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock(side_effect=RuntimeError("crash"))
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        session.clients.close.assert_awaited()

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_resets_lifecycle_metrics_on_completion(self, mock_tracer_mod, mock_factory, mock_metrics):
        session = _make_session(game_kind=GAME_KIND_PRIMARY)
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        # active_game/active_players reset to 0 for this session's kind.
        mock_metrics.active_game.labels.assert_any_call(
            game_kind=GAME_KIND_PRIMARY, game_id="game_abc123", experiment_id="", arm=""
        )
        mock_metrics.active_game.labels.return_value.set.assert_any_call(0)
        mock_metrics.active_players.labels.return_value.set.assert_any_call(0)
        # players_alive (still a single global gauge) reset only by primary.
        mock_metrics.players_alive.set.assert_any_call(0)

    def test_eval_game_kind_maps_session_kind_to_real_or_shadow(self):
        """Session kind (primary/shadow, #775) maps to eval game_kind (real/shadow,
        #932): only shadow resolves experiments, primary is the protected "real"."""
        assert _make_session(game_kind=GAME_KIND_PRIMARY)._eval_game_kind() == "real"
        assert _make_session(game_kind=GAME_KIND_SHADOW)._eval_game_kind() == "shadow"

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.set_game_session_kind_context")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_sets_shadow_eval_context_before_create_game(
        self, mock_tracer_mod, mock_factory, mock_set_kind, mock_metrics
    ):
        """A shadow session establishes game_kind="shadow" in its async context
        BEFORE GameFactory.create_game() runs __init__-time calibration reads, so
        those reads see the shadow split (#932). Order is asserted via a sentinel."""
        order = []
        mock_set_kind.side_effect = lambda kind, **_kw: order.append(("set_kind", kind))
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()

        def _record_create(*_a, **_k):
            order.append(("create_game", None))
            return mock_game

        mock_factory.create_game.side_effect = _record_create

        await session._run_game_loop_async()

        assert order[0] == ("set_kind", "shadow")
        assert ("create_game", None) in order
        assert order.index(("set_kind", "shadow")) < order.index(("create_game", None))

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.set_game_session_kind_context")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_sets_real_eval_context_for_primary(
        self, mock_tracer_mod, mock_factory, mock_set_kind, mock_metrics
    ):
        """A primary (menu) session establishes the protected game_kind="real"."""
        session = _make_session(game_kind=GAME_KIND_PRIMARY)
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        mock_set_kind.assert_called_once_with("real", experiment_id=None, arm=None)

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_increments_completed_total_with_game_kind(self, mock_tracer_mod, mock_factory, mock_metrics):
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        mock_metrics.games_completed_total.labels.assert_called_with(mode="FFA", game_kind=GAME_KIND_SHADOW)
        mock_metrics.games_completed_total.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_emits_experiment_span_attrs(self, mock_tracer_mod, mock_factory, mock_metrics):
        """The game span carries experiment.id + experiment.arm (#975), the primary
        attribution channel. A bound experiment game stamps both."""
        session = _make_session(game_kind=GAME_KIND_SHADOW, experiment_id="exp_abc123", arm="experimental")
        span = MockSpan()
        mock_tracer_mod.start_as_current_span.return_value.__enter__ = MagicMock(return_value=span)
        mock_tracer_mod.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        assert span.attributes["experiment.id"] == "exp_abc123"
        assert span.attributes["experiment.arm"] == "experimental"
        # The game object is threaded the attribution for its in-loop flag eval.
        assert mock_game.experiment_id == "exp_abc123"
        assert mock_game.arm == "experimental"

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_emits_empty_experiment_attrs_for_non_experiment(
        self, mock_tracer_mod, mock_factory, mock_metrics
    ):
        """A non-experiment game stamps empty experiment.id/arm — present but blank,
        mirroring game.kind, so the agent's no-op-on-empty setters leave it alone."""
        session = _make_session(game_kind=GAME_KIND_PRIMARY)
        span = MockSpan()
        mock_tracer_mod.start_as_current_span.return_value.__enter__ = MagicMock(return_value=span)
        mock_tracer_mod.start_as_current_span.return_value.__exit__ = MagicMock(return_value=False)
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        assert span.attributes["experiment.id"] == ""
        assert span.attributes["experiment.arm"] == ""

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_labels_lifecycle_metrics_with_experiment(self, mock_tracer_mod, mock_factory, mock_metrics):
        """An experiment game labels its per-live-game GAUGES with experiment_id +
        arm (#975 cardinality decision: gauges are keyed on the unbounded game_id
        and set per live game, so they add no permanent series). The cumulative
        COUNTERS deliberately do NOT carry experiment_id/arm — a label on a
        cumulative counter is one permanent series per experiment, forever."""
        session = _make_session(game_kind=GAME_KIND_SHADOW, experiment_id="exp_abc123", arm="control")
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        mock_metrics.active_game.labels.assert_any_call(
            game_kind=GAME_KIND_SHADOW, game_id="game_abc123", experiment_id="exp_abc123", arm="control"
        )
        # Cumulative counter: mode x game_kind only, no experiment labels.
        mock_metrics.games_completed_total.labels.assert_called_with(mode="FFA", game_kind=GAME_KIND_SHADOW)
        mock_metrics.game_duration_seconds.labels.assert_any_call(
            game_kind=GAME_KIND_SHADOW, game_id="game_abc123", experiment_id="exp_abc123", arm="control"
        )

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_emits_trace_correlation_gauge(self, mock_tracer_mod, mock_factory, mock_metrics):
        """#1133/#1157: while the game span is live the loop publishes
        game_trace_correlation carrying game_id + the span's hex trace_id AND its
        hex span_id, so the agent can link agent.decision -> this exact game span.
        Both hex ids are captured onto the session."""
        from opentelemetry import trace as ot_trace

        session = _make_session(game_kind=GAME_KIND_SHADOW)
        trace_id = 0x4BF92F3577B34DA6A3CE929D0E0E4736
        span_id = 0x051581BF3CB55C13
        mock_tracer_mod.start_as_current_span = _tracer_mock(trace_id=trace_id, span_id=span_id).start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        expected_trace_hex = ot_trace.format_trace_id(trace_id)
        expected_span_hex = ot_trace.format_span_id(span_id)
        assert session.game_trace_id == expected_trace_hex
        assert session.game_trace_span_id == expected_span_hex
        # The root-span emit carries an EMPTY gameplay_phase_span_id (#1195): the
        # gameplay_phase span opens later inside game.run(), and the mocked game here
        # never opens it, so it stays "".
        mock_metrics.game_trace_correlation.labels.assert_any_call(
            game_kind=GAME_KIND_SHADOW,
            game_id="game_abc123",
            game_trace_id=expected_trace_hex,
            game_trace_span_id=expected_span_hex,
            gameplay_phase_span_id="",
        )
        mock_metrics.game_trace_correlation.labels.return_value.set.assert_any_call(1)

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_skips_trace_correlation_for_unsampled_span(self, mock_tracer_mod, mock_factory, mock_metrics):
        """#1133 fallback: an unsampled/non-recording span reports the all-zero invalid
        trace id; the loop must NOT emit the correlation gauge (no link to an invalid
        trace) and must leave game_trace_id empty — no crash."""
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        mock_tracer_mod.start_as_current_span = _tracer_mock(trace_id=0).start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        assert session.game_trace_id == ""
        assert session.game_trace_span_id == ""
        mock_metrics.game_trace_correlation.labels.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_game_span_is_rooted_with_link_to_stream(self, mock_tracer_mod, mock_factory, mock_metrics):
        """#1157: the game-lifecycle span is started as a NEW ROOT (explicit empty
        Context, never the inbound stream span's context) with an OTel Link back to
        the inbound stream/start-RPC SpanContext — keeping the call navigable
        without parenting the game under the long-lived stream trace."""
        from opentelemetry.context import Context

        stream_ctx = MockSpanContext(trace_id=0x11112222333344445555666677778888, span_id=0xAABBCCDDEEFF0011)
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        session.stream_span_context = stream_ctx
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        _, kwargs = mock_tracer_mod.start_as_current_span.call_args
        # Rooted as its own trace: an explicit empty Context, NOT the stream context.
        assert isinstance(kwargs["context"], Context)
        assert len(kwargs["context"]) == 0
        # Exactly one Link, pointing at the inbound stream SpanContext.
        links = kwargs["links"]
        assert len(links) == 1
        assert links[0].context is stream_ctx

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_game_span_rooted_without_link_when_no_stream_context(
        self, mock_tracer_mod, mock_factory, mock_metrics
    ):
        """#1157: with no recordable inbound span (telemetry off / unsampled stream),
        the game span is still rooted but carries NO Link (never link an invalid
        context). stream_span_context=None and trace_id==0 are both treated as 'none'."""
        from opentelemetry.context import Context

        for stream_ctx in (None, MockSpanContext(trace_id=0, span_id=0)):
            session = _make_session(game_kind=GAME_KIND_SHADOW)
            session.stream_span_context = stream_ctx
            mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
            mock_game = MagicMock()
            mock_game.run = AsyncMock()
            mock_factory.create_game.return_value = mock_game

            await session._run_game_loop_async()

            _, kwargs = mock_tracer_mod.start_as_current_span.call_args
            assert isinstance(kwargs["context"], Context)
            assert kwargs["links"] == []

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_shadow_experiment_game_span_is_child_of_injected_parent(
        self, mock_tracer_mod, mock_factory, mock_metrics
    ):
        """#1182: a SHADOW + experiment-bound game whose spawn carried the agent's
        experiment traceparent starts its game span as a CHILD of that remote parent
        (causal parent-child), so it nests under the long-lived experiment span. The
        parent context is a real, NON-empty Context containing the experiment span."""
        from opentelemetry import trace as ot_trace
        from opentelemetry.context import Context

        exp_trace_id = 0x9999AAAABBBBCCCCDDDDEEEEFFFF0000
        exp_span_id = 0x1122334455667788
        parent_ctx = MockSpanContext(trace_id=exp_trace_id, span_id=exp_span_id)

        session = _make_session(game_kind=GAME_KIND_SHADOW, experiment_id="exp-1", arm="experimental")
        session.experiment_parent_span_context = parent_ctx
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        _, kwargs = mock_tracer_mod.start_as_current_span.call_args
        ctx = kwargs["context"]
        # NOT own-root: a non-empty Context carrying the experiment span as parent.
        assert isinstance(ctx, Context)
        assert len(ctx) > 0, "shadow+experiment game must be parented, not own-rooted"
        parent_span = ot_trace.get_current_span(ctx)
        sc = parent_span.get_span_context()
        assert sc.trace_id == exp_trace_id
        assert sc.span_id == exp_span_id

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_real_game_stays_own_root_even_with_parent_context(self, mock_tracer_mod, mock_factory, mock_metrics):
        """#1182 real-game protection: a PRIMARY (real) game is own-rooted (#1157/#1164)
        even if an experiment parent context and experiment_id are erroneously present.
        The strict shadow+experiment gate keeps the real-game trace structure UNCHANGED."""
        from opentelemetry.context import Context

        parent_ctx = MockSpanContext(trace_id=0x9999AAAABBBBCCCCDDDDEEEEFFFF0000, span_id=0x1122334455667788)
        # Real (primary) kind. The servicer zeroes experiment_id for a real game; even
        # if a parent context leaked through, the kind gate alone must keep it own-root.
        session = _make_session(game_kind=GAME_KIND_PRIMARY, experiment_id="exp-1")
        session.experiment_parent_span_context = parent_ctx
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        _, kwargs = mock_tracer_mod.start_as_current_span.call_args
        ctx = kwargs["context"]
        assert isinstance(ctx, Context)
        assert len(ctx) == 0, "real game must stay own-rooted (#1157/#1164), never parented"

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_shadow_experiment_game_own_root_when_no_parent(self, mock_tracer_mod, mock_factory, mock_metrics):
        """#1182 graceful: a shadow + experiment game with NO injected parent
        (telemetry off, or no traceparent on the spawn) stays own-rooted — the
        own-root #1164 path is preserved when there is nothing valid to parent under.
        Covers both None and an invalid (trace_id==0) parent context."""
        from opentelemetry.context import Context

        for parent_ctx in (None, MockSpanContext(trace_id=0, span_id=0)):
            session = _make_session(game_kind=GAME_KIND_SHADOW, experiment_id="exp-1", arm="experimental")
            session.experiment_parent_span_context = parent_ctx
            mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
            mock_game = MagicMock()
            mock_game.run = AsyncMock()
            mock_factory.create_game.return_value = mock_game

            await session._run_game_loop_async()

            _, kwargs = mock_tracer_mod.start_as_current_span.call_args
            ctx = kwargs["context"]
            assert isinstance(ctx, Context)
            assert len(ctx) == 0, "shadow+experiment with no valid parent must stay own-rooted"

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.metrics")
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_shadow_loop_does_not_reset_players_alive(self, mock_tracer_mod, mock_factory, mock_metrics):
        """A shadow session ending must not reset the global players_alive gauge."""
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        mock_metrics.players_alive.set.assert_not_called()

    @pytest.mark.asyncio
    @patch("services.game_coordinator.game_session.GameFactory")
    @patch("services.game_coordinator.game_session.tracer")
    async def test_loop_tags_game_with_kind_and_gauge_flag(self, mock_tracer_mod, mock_factory):
        """The game instance is tagged so its end-cleanup is per-session."""
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        mock_tracer_mod.start_as_current_span = _tracer_mock().start_as_current_span
        mock_game = MagicMock()
        mock_game.run = AsyncMock()
        mock_factory.create_game.return_value = mock_game

        await session._run_game_loop_async()

        assert mock_game.game_kind == GAME_KIND_SHADOW
        # Shadow session must NOT reset global gauges on end.
        assert mock_game._reset_global_gauges_on_end is False


class TestGameSessionForceEnd:
    @pytest.mark.asyncio
    async def test_force_end_no_game(self):
        session = _make_session()
        session.game_state = game_coordinator_pb2.GameState.IDLE

        ok, error = await session.force_end("test")

        assert ok is False
        assert "no game in progress" in error.lower()

    @pytest.mark.asyncio
    async def test_force_end_running_game(self):
        session = _make_session()
        session.game_state = game_coordinator_pb2.GameState.RUNNING
        session.game_running = True
        mock_game = MagicMock()
        session.current_game = mock_game

        ok, error = await session.force_end("user requested")

        assert ok is True
        assert error == ""
        mock_game.force_end.assert_called_once()
        assert session.game_state == game_coordinator_pb2.GameState.ENDED
        session.event_bus.publish.assert_awaited()

    @pytest.mark.asyncio
    async def test_state_sync_updates_own_state(self):
        from lib.types import GameEvent

        session = _make_session()
        session.game_state = game_coordinator_pb2.GameState.STARTING

        session.on_event_state_sync(GameEvent.GAME_STARTED)
        assert session.game_state == game_coordinator_pb2.GameState.RUNNING

        session.on_event_state_sync(GameEvent.GAME_ENDED)
        assert session.game_state == game_coordinator_pb2.GameState.ENDED


class TestGameSessionClearMetrics:
    """clear_metrics() removes this session's game_id-labeled gauge series on
    retire so zeroed-but-persistent series don't accumulate as shadow games
    churn short-lived game_ids (#1018)."""

    @patch("services.game_coordinator.game_session.metrics")
    def test_clear_metrics_removes_exact_label_tuple(self, mock_metrics):
        session = _make_session(game_kind=GAME_KIND_SHADOW, experiment_id="exp_abc123", arm="experimental")

        session.clear_metrics()

        # Each game_id-labeled gauge removed with the EXACT tuple it was set with
        # (game_kind, game_id, experiment_id, arm), in declaration order.
        expected = (GAME_KIND_SHADOW, "game_abc123", "exp_abc123", "experimental")
        mock_metrics.active_game.remove.assert_called_once_with(*expected)
        mock_metrics.active_players.remove.assert_called_once_with(*expected)
        mock_metrics.game_duration_seconds.remove.assert_called_once_with(*expected)

    @patch("services.game_coordinator.game_session.metrics")
    def test_clear_metrics_primary_uses_empty_experiment_labels(self, mock_metrics):
        session = _make_session(game_kind=GAME_KIND_PRIMARY)

        session.clear_metrics()

        expected = (GAME_KIND_PRIMARY, "game_abc123", "", "")
        mock_metrics.active_game.remove.assert_called_once_with(*expected)

    @patch("services.game_coordinator.game_session.metrics")
    def test_clear_metrics_removes_trace_correlation_when_set(self, mock_metrics):
        """#1133/#1157/#1195: the trace-correlation gauge (labels game_kind, game_id,
        game_trace_id, game_trace_span_id, gameplay_phase_span_id) is removed at retire
        ONLY when a trace id was captured — otherwise it was never set. Removes with the
        CURRENT gameplay_phase_span_id."""
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        session.game_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        session.game_trace_span_id = "051581bf3cb55c13"
        session.gameplay_phase_span_id = "00f067aa0ba902b7"

        session.clear_metrics()

        mock_metrics.game_trace_correlation.remove.assert_called_once_with(
            GAME_KIND_SHADOW,
            "game_abc123",
            "4bf92f3577b34da6a3ce929d0e0e4736",
            "051581bf3cb55c13",
            "00f067aa0ba902b7",
        )

    @patch("services.game_coordinator.game_session.metrics")
    def test_clear_metrics_skips_trace_correlation_when_unset(self, mock_metrics):
        """#1133: an unsampled span never set the correlation gauge, so retire must
        not try to remove a series that was never created."""
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        # game_trace_id stays "" (default) — span was never sampled.

        session.clear_metrics()

        mock_metrics.game_trace_correlation.remove.assert_not_called()


class TestPublishGameplayPhaseSpanID:
    """#1195: when the gameplay_phase span opens, the game calls
    publish_gameplay_phase_span_id; the session re-emits the correlation gauge
    swapping the empty gameplay_phase_span_id series for the populated one, so the
    agent re-parents its in-game decision chain under gameplay_phase."""

    @patch("services.game_coordinator.game_session.metrics")
    def test_publish_swaps_empty_series_for_populated(self, mock_metrics):
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        # Simulate the root-span emit having run (gauge set with empty gameplay id).
        session.game_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        session.game_trace_span_id = "051581bf3cb55c13"

        session.publish_gameplay_phase_span_id("00f067aa0ba902b7")

        # Empty-gameplay series removed first ...
        mock_metrics.game_trace_correlation.remove.assert_called_once_with(
            GAME_KIND_SHADOW,
            "game_abc123",
            "4bf92f3577b34da6a3ce929d0e0e4736",
            "051581bf3cb55c13",
            "",
        )
        # ... then the populated series set.
        mock_metrics.game_trace_correlation.labels.assert_called_once_with(
            game_kind=GAME_KIND_SHADOW,
            game_id="game_abc123",
            game_trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            game_trace_span_id="051581bf3cb55c13",
            gameplay_phase_span_id="00f067aa0ba902b7",
        )
        mock_metrics.game_trace_correlation.labels.return_value.set.assert_called_once_with(1)
        assert session.gameplay_phase_span_id == "00f067aa0ba902b7"

    @patch("services.game_coordinator.game_session.metrics")
    def test_publish_noop_when_game_trace_unsampled(self, mock_metrics):
        """No gauge was ever set (game_trace_id empty) -> publish is a no-op so the
        agent keeps falling back to the (absent) root; gameplay id stays empty."""
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        # game_trace_id stays "" (unsampled).

        session.publish_gameplay_phase_span_id("00f067aa0ba902b7")

        mock_metrics.game_trace_correlation.remove.assert_not_called()
        mock_metrics.game_trace_correlation.labels.assert_not_called()
        assert session.gameplay_phase_span_id == ""

    @patch("services.game_coordinator.game_session.metrics")
    def test_publish_noop_when_span_id_empty(self, mock_metrics):
        """An empty/unsampled gameplay_phase span_id is a no-op (two-tier fallback to
        the root span on the agent side)."""
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        session.game_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        session.game_trace_span_id = "051581bf3cb55c13"

        session.publish_gameplay_phase_span_id("")

        mock_metrics.game_trace_correlation.remove.assert_not_called()
        mock_metrics.game_trace_correlation.labels.assert_not_called()
        assert session.gameplay_phase_span_id == ""

    def test_clear_metrics_idempotent_on_missing_series(self):
        """A double-retire (or never-started session) must not raise — the gauge
        client pops missing series silently and remove() is KeyError/Value-guarded."""
        session = _make_session(game_kind=GAME_KIND_SHADOW)

        # Real metrics module (no series ever set for this game_id).
        session.clear_metrics()
        session.clear_metrics()  # second call must also be safe


class TestGameSessionInit:
    def test_primary_is_primary(self):
        session = _make_session(game_kind=GAME_KIND_PRIMARY)
        assert session.is_primary is True

    def test_shadow_is_not_primary(self):
        session = _make_session(game_kind=GAME_KIND_SHADOW)
        assert session.is_primary is False

    def test_initial_state_is_starting(self):
        session = _make_session()
        assert session.game_state == game_coordinator_pb2.GameState.STARTING
        assert session.current_game is None
        assert session.game_running is False
        assert isinstance(session.game_start_time, float)
        assert session.game_start_time <= time.time()
