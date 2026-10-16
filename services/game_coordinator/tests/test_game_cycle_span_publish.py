"""The coordinator exports the game_cycle span id to the agent (#1325).

UC-041 BR-004: the game_trace_correlation gauge carries the game_cycle span id once
that span opens, so the agent can parent its in-game spans under game_cycle
(UC-040 BR-013). Two layers are covered:

- base.py: when ``run()`` opens game_cycle it calls ``on_game_cycle_open`` with that
  span's hex id (asserted against a real SDK tracer + in-memory exporter);
- game_session.py: ``publish_game_cycle_span_id`` swaps the correlation series for
  one carrying the id, and retire removes that exact series.
"""

import contextlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from proto import game_coordinator_pb2
from services.game_coordinator.event_bus import EventBus
from services.game_coordinator.game_session import GAME_KIND_SHADOW, GameSession
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.tests.conftest import EventCollector, MockControllerManagerService

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
ROOT_SPAN_ID = "051581bf3cb55c13"
GAMEPLAY_SPAN_ID = "00f067aa0ba902b7"
CYCLE_SPAN_ID = "1a2b3c4d5e6f7081"


def _sampled_session() -> GameSession:
    """A session whose root game span was sampled (the gauge was set at game start)."""
    session = GameSession(
        game_id="game_abc123",
        game_name="FFA",
        players=[],
        game_config=game_coordinator_pb2.StartGameConfig(sensitivity=2),
        event_bus=create_autospec(EventBus, instance=True),
        game_kind=GAME_KIND_SHADOW,
        stream_span_context=None,
    )
    session.game_trace_id = TRACE_ID
    session.game_trace_span_id = ROOT_SPAN_ID
    return session


@pytest.fixture
def span_exporter(monkeypatch):
    """Route base.py's module tracer to a real SDK tracer with an in-memory exporter."""
    # conftest disables the SDK via OTEL_SDK_DISABLED, which a TracerProvider reads at
    # construction; lift it for this provider only.
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with patch("services.game_coordinator.games.base.tracer", provider.get_tracer("test")):
        yield exporter


def _fast_ffa_game() -> FFAGame:
    """An FFA game whose countdown, warm-up and loop are stubbed so run() is quick."""
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=3),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id="game_cycle_publish",
        initial_players=[type("P", (), {"serial": f"mock_controller_{i}"})() for i in range(3)],
    )
    game._countdown = AsyncMock()
    game._warmup_ema = AsyncMock()
    game._game_loop = AsyncMock()
    return game


@pytest.mark.uc("UC-041", br=["BR-004"])
@pytest.mark.asyncio
async def test_run_publishes_game_cycle_span_id(span_exporter):
    """run() reports the id of the span it opened as game_cycle, a child of gameplay_phase."""
    game = _fast_ffa_game()
    cycle_ids: list[str] = []
    gameplay_ids: list[str] = []
    game.on_game_cycle_open = cycle_ids.append
    game.on_gameplay_phase_open = gameplay_ids.append

    await game.run()

    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    cycle = spans["game_cycle"]
    gameplay = spans["gameplay_phase"]
    assert cycle_ids == [trace.format_span_id(cycle.context.span_id)]
    assert gameplay_ids == [trace.format_span_id(gameplay.context.span_id)]
    assert cycle.parent.span_id == gameplay.context.span_id
    assert cycle_ids[0] != gameplay_ids[0]


@pytest.mark.uc("UC-041", br=["BR-004"])
@pytest.mark.asyncio
async def test_run_skips_publish_when_game_cycle_unsampled():
    """A non-recording (unsampled) game_cycle has span id 0: nothing is published."""
    game = _fast_ffa_game()
    cycle_ids: list[str] = []
    game.on_game_cycle_open = cycle_ids.append

    # The conftest disables telemetry, so the module tracer yields non-recording spans.
    with contextlib.suppress(Exception):
        await game.run()

    assert cycle_ids == []


@pytest.mark.uc("UC-041", br=["BR-004"])
@patch("services.game_coordinator.game_session.metrics")
def test_publish_game_cycle_swaps_series(mock_metrics):
    """The series with the gameplay_phase id is replaced by one that also carries game_cycle."""
    session = _sampled_session()
    session.gameplay_phase_span_id = GAMEPLAY_SPAN_ID

    session.publish_game_cycle_span_id(CYCLE_SPAN_ID)

    mock_metrics.game_trace_correlation.remove.assert_called_once_with(
        GAME_KIND_SHADOW, "game_abc123", TRACE_ID, ROOT_SPAN_ID, GAMEPLAY_SPAN_ID, ""
    )
    mock_metrics.game_trace_correlation.labels.assert_called_once_with(
        game_kind=GAME_KIND_SHADOW,
        game_id="game_abc123",
        game_trace_id=TRACE_ID,
        game_trace_span_id=ROOT_SPAN_ID,
        gameplay_phase_span_id=GAMEPLAY_SPAN_ID,
        game_cycle_span_id=CYCLE_SPAN_ID,
    )
    mock_metrics.game_trace_correlation.labels.return_value.set.assert_called_once_with(1)
    assert session.game_cycle_span_id == CYCLE_SPAN_ID


@pytest.mark.uc("UC-041", br=["BR-004"])
@pytest.mark.parametrize(
    ("trace_id", "span_id"),
    [("", CYCLE_SPAN_ID), (TRACE_ID, "")],
    ids=["unsampled_game", "empty_id"],
)
@patch("services.game_coordinator.game_session.metrics")
def test_publish_game_cycle_noop(mock_metrics, trace_id, span_id):
    """No gauge was set (unsampled game) or no id to publish: nothing changes."""
    session = _sampled_session()
    session.game_trace_id = trace_id

    session.publish_game_cycle_span_id(span_id)

    mock_metrics.game_trace_correlation.remove.assert_not_called()
    mock_metrics.game_trace_correlation.labels.assert_not_called()
    assert session.game_cycle_span_id == ""


@pytest.mark.uc("UC-041", br=["BR-004"])
@patch("services.game_coordinator.game_session.metrics")
def test_publish_game_cycle_same_id_is_noop(mock_metrics):
    """Re-publishing the id already on the series does not churn it."""
    session = _sampled_session()
    session.game_cycle_span_id = CYCLE_SPAN_ID

    session.publish_game_cycle_span_id(CYCLE_SPAN_ID)

    mock_metrics.game_trace_correlation.remove.assert_not_called()
    mock_metrics.game_trace_correlation.labels.assert_not_called()


class _RecordingGauge:
    """Stand-in for the correlation gauge that keeps its live series (the real gauge
    drops writes while metrics are disabled for tests). Keyed by the label tuple in
    the gauge's label order, as ``remove`` takes it."""

    LABELS = (
        "game_kind",
        "game_id",
        "game_trace_id",
        "game_trace_span_id",
        "gameplay_phase_span_id",
        "game_cycle_span_id",
    )

    def __init__(self):
        self.series: dict[tuple, float] = {}

    def labels(self, **labels):
        key = tuple(labels[name] for name in self.LABELS)
        gauge = self

        class _Series:
            def set(self, value):
                gauge.series[key] = value

        return _Series()

    def remove(self, *values):
        if len(values) != len(self.LABELS):
            raise ValueError("label count")
        del self.series[values]


@pytest.mark.uc("UC-041", br=["BR-004"])
def test_retire_removes_the_series_carrying_game_cycle():
    """After both phase publishes exactly one series remains, carrying both ids, and
    clear_metrics removes it."""
    gauge = _RecordingGauge()
    session = _sampled_session()
    with patch("services.game_coordinator.game_session.metrics.game_trace_correlation", gauge):
        session._set_trace_correlation()
        session.publish_gameplay_phase_span_id(GAMEPLAY_SPAN_ID)
        session.publish_game_cycle_span_id(CYCLE_SPAN_ID)

        assert gauge.series == {
            (GAME_KIND_SHADOW, "game_abc123", TRACE_ID, ROOT_SPAN_ID, GAMEPLAY_SPAN_ID, CYCLE_SPAN_ID): 1,
        }

        session.clear_metrics()

    assert gauge.series == {}
