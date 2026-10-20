"""The trace an operator follows in Jaeger: root span, phases, one span per player (UC-035 step 5).

A whole FFA session runs through ``GameSession._run_game_loop_async`` with a real SDK tracer and an
in-memory exporter. The collected spans must form one trace: a root named after the mode, the phase
spans under it, and one ``player_lifecycle`` span per player under the ``players`` group.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from lib.types import get_game_display_name
from services.game_coordinator.tests.test_game_cycle_span_publish import _fast_ffa_game
from services.game_coordinator.tests.test_game_session import _make_session


@pytest.fixture
def span_exporter(monkeypatch):
    """Route the session's and the game's module tracers to one real SDK tracer."""
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with (
        patch("services.game_coordinator.game_session.tracer", tracer),
        patch("services.game_coordinator.games.base.tracer", tracer),
    ):
        yield exporter


@pytest.mark.uc("UC-035", flow="main", steps=[5])
@pytest.mark.asyncio
@patch("services.game_coordinator.game_session.GameFactory")
async def test_game_trace_has_mode_root_phases_and_one_span_per_player(mock_factory, span_exporter):
    session = _make_session()
    session.game_running = True
    game = _fast_ffa_game()
    mock_factory.create_game.return_value = game
    session.clients.close = AsyncMock()

    await session._run_game_loop_async()

    spans = span_exporter.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    roots = [s for s in spans if s.parent is None]
    assert [r.name for r in roots] == [get_game_display_name("FFA")]
    root = roots[0]
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}

    def parent_name(span):
        return by_id[span.parent.span_id].name

    for phase in ("initialization_phase", "gameplay_phase", "teardown_phase"):
        (span,) = [s for s in spans if s.name == phase]
        assert span.parent.span_id == root.context.span_id, phase

    players_group = next(s for s in spans if s.name == "players")
    assert parent_name(players_group) == "gameplay_phase"
    lifecycles = [s for s in spans if s.name == "player_lifecycle"]
    assert len(lifecycles) == len(game.players) == 3
    assert {s.parent.span_id for s in lifecycles} == {players_group.context.span_id}
    assert {s.attributes["player.serial"] for s in lifecycles} == set(game.players)


@pytest.mark.uc("UC-035", br=["BR-002"])
@pytest.mark.parametrize("game_kind", ["primary", "shadow"])
@pytest.mark.asyncio
@patch("services.game_coordinator.game_session.GameFactory")
async def test_root_span_names_game_kind_in_coordinator_vocabulary(mock_factory, span_exporter, game_kind):
    """BR-002: coordinator spans say primary / shadow, never the agent's `real`."""
    session = _make_session(game_kind=game_kind)
    session.game_running = True
    mock_factory.create_game.return_value = _fast_ffa_game()
    session.clients.close = AsyncMock()

    await session._run_game_loop_async()

    (root,) = [s for s in span_exporter.get_finished_spans() if s.parent is None]
    assert root.attributes["game.kind"] == game_kind
    assert root.attributes["game.kind"] != "real"
