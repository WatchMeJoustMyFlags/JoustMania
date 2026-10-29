"""Player lifecycle spans end on every run() exit path (#1997, telemetry.md §3.2, UC-030 BR-007).

Fight Club, Werewolf and Zombies return early from their own ``run()`` after a
force-end during the intro, the countdown or the warm-up; the base ``run()``
(JoustFFA, NonStop) leaves the warm-up outside its span-closing ``finally``.
Those paths skip the mode's end step, so the shared run-end cleanup ends every
player span still open: ``game_ended`` with ``winner`` false and
``game.outcome`` ``force_ended`` (or ``error``). A natural end is unchanged:
each span ends once, by the mode's own end step.
"""

import asyncio
import inspect
import logging
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from lib.types import Games
from services.game_coordinator.game_factory import _GAME_CLASSES
from services.game_coordinator.games import (
    base,
    fight_club,
    nonstop_joust,
    swapper,
    teams_base,
    tournament,
    traitor,
    werewolf,
    zombie,
)
from services.game_coordinator.games.base import Phase
from services.game_coordinator.runtime_config import GamePerformanceConfig, get_config_manager

TRACED_MODULES = [base, fight_club, nonstop_joust, swapper, teams_base, tournament, traitor, werewolf, zombie]
RUN_OVERRIDE_MODES = [Games.FightClub, Games.Werewolf, Games.Zombies, Games.Tournament]
BASE_RUN_MODES = [Games.JoustFFA, Games.NonStop]
EARLY_RETURN_PHASES = ["intro", "countdown", "warmup"]
# Werewolf and Zombies return early after the intro and the countdown only: a force-end
# during the warm-up still runs their (empty) game loop and their end step.
WOLF_ZOMBIE_PHASES = ["intro", "countdown", "warmup_end_step"]
SERIALS = [f"mock_controller_{i}" for i in range(4)]
DOUBLE_END = "Calling end() on an ended span"


class _StartedSpans(SpanProcessor):
    """Records every started span, so a test can find the spans that never ended."""

    def __init__(self):
        self.spans = []

    def on_start(self, span, parent_context=None):
        self.spans.append(span)


@pytest.fixture
def exporter(monkeypatch):
    """An SDK tracer behind every game module, and no countdown or long rainbow."""
    # conftest disables the SDK via OTEL_SDK_DISABLED, which a TracerProvider reads at construction.
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    exp = InMemorySpanExporter()
    exp.started = _StartedSpans()
    provider = TracerProvider()
    provider.add_span_processor(exp.started)
    provider.add_span_processor(SimpleSpanProcessor(exp))
    tracer = provider.get_tracer("test_run_exit_player_spans")
    config = GamePerformanceConfig(countdown_phase_duration_ms=0, winner_rainbow_duration_ms=50)
    with ExitStack() as stack:
        for module in TRACED_MODULES:
            stack.enter_context(patch.object(module, "tracer", tracer))
        stack.enter_context(patch.object(get_config_manager(), "get_config", return_value=config))
        yield exp


def _game(mode: Games):
    return _GAME_CLASSES[mode](
        controller_manager_client=MockControllerManagerService(num_controllers=4, infinite=True),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id=f"test_exit_spans_{mode.name.lower()}",
        initial_players=[type("P", (), {"serial": s})() for s in SERIALS],
    )


def _force_end_after(game, phase: str) -> None:
    """Force-end the game right after ``phase`` (the intro is replaced by a stub phase)."""
    if phase == "intro":

        async def intro():
            game.force_end("test_intro")

        game._get_additional_phases = lambda: [Phase(name="test_intro", execute=intro)]
        return
    attr = {"countdown": "_countdown", "warmup": "_warmup_ema"}[phase]
    real = getattr(game, attr)

    async def wrapped():
        await real()
        game.force_end(f"test_{phase}")

    setattr(game, attr, wrapped)


def _player_spans(exp) -> list:
    return [s for s in exp.get_finished_spans() if s.name == "player_lifecycle" or s.name.startswith("player: ")]


def _assert_no_span_left_open(exp) -> None:
    """Every span the run started ended: player, countdown_phase and the grouping spans."""
    open_spans = sorted(s.name for s in exp.started.spans if s.end_time is None)
    assert open_spans == [], f"spans left open: {open_spans}"


def _game_ended(span) -> list:
    return [e for e in span.events if e.name == "game_ended"]


def _assert_every_player_span_ended_once(exp, caplog, outcome: str | None, reason: str | None) -> None:
    """``outcome`` None: the mode's own end step closed the spans (no ``game.outcome`` attribute)."""
    spans = _player_spans(exp)
    assert sorted(s.attributes["player.serial"] for s in spans) == SERIALS, "one ended span per player"
    for span in spans:
        events = _game_ended(span)
        assert len(events) == 1, span.events
        attrs = dict(events[0].attributes)
        assert attrs["winner"] is False
        assert attrs.get("game.outcome") == outcome
        assert attrs.get("game.end_reason") == reason
    assert DOUBLE_END not in caplog.text
    _assert_no_span_left_open(exp)


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.uc("UC-025", flow="A2", steps=[1], br=["BR-003"])
@pytest.mark.parametrize("phase", EARLY_RETURN_PHASES)
@pytest.mark.asyncio
async def test_fight_club_force_end_before_the_rounds_ends_every_player_span(exporter, caplog, phase):
    await _run_force_ended(Games.FightClub, phase, exporter, caplog)


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.uc("UC-022", br=["BR-004"])
@pytest.mark.parametrize("phase", WOLF_ZOMBIE_PHASES)
@pytest.mark.asyncio
async def test_werewolf_force_end_before_the_game_loop_ends_every_player_span(exporter, caplog, phase):
    await _run_force_ended(Games.Werewolf, phase, exporter, caplog)


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.uc("UC-023", br=["BR-003"])
@pytest.mark.parametrize("phase", WOLF_ZOMBIE_PHASES)
@pytest.mark.asyncio
async def test_zombies_force_end_before_the_game_loop_ends_every_player_span(exporter, caplog, phase):
    await _run_force_ended(Games.Zombies, phase, exporter, caplog)


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.parametrize("phase", EARLY_RETURN_PHASES)
@pytest.mark.asyncio
async def test_tournament_force_end_before_the_bracket_ends_every_player_span(exporter, caplog, phase):
    await _run_force_ended(Games.Tournament, phase, exporter, caplog)


async def _run_force_ended(mode: Games, phase: str, exp, caplog) -> None:
    caplog.set_level(logging.WARNING)
    game = _game(mode)
    by_end_step = phase == "warmup_end_step"
    phase = "warmup" if by_end_step else phase
    _force_end_after(game, phase)
    await asyncio.wait_for(game.run(), timeout=20)

    if by_end_step:
        _assert_every_player_span_ended_once(exp, caplog, None, None)
    else:
        _assert_every_player_span_ended_once(exp, caplog, "force_ended", f"test_{phase}")
    assert all(s.status.status_code == StatusCode.OK for s in _player_spans(exp))
    assert all(p.span is None for p in game.players.values())


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.parametrize("mode", RUN_OVERRIDE_MODES + BASE_RUN_MODES, ids=lambda m: m.name)
@pytest.mark.asyncio
async def test_an_error_in_the_warmup_ends_every_player_span(exporter, caplog, mode):
    caplog.set_level(logging.WARNING)
    game = _game(mode)

    async def broken_warmup():
        raise RuntimeError("warm-up failed")

    game._warmup_ema = broken_warmup
    with pytest.raises(RuntimeError, match="warm-up failed"):
        await asyncio.wait_for(game.run(), timeout=20)

    _assert_every_player_span_ended_once(exporter, caplog, "error", None)
    assert all(s.status.status_code == StatusCode.ERROR for s in _player_spans(exporter))
    names = {s.name for s in exporter.get_finished_spans()}
    if mode in BASE_RUN_MODES:
        assert {"players", "game_cycle"} <= names, "the grouping spans end with the player spans"


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.parametrize("mode", BASE_RUN_MODES, ids=lambda m: m.name)
@pytest.mark.asyncio
async def test_an_error_in_the_countdown_ends_the_countdown_and_grouping_spans(exporter, caplog, mode):
    caplog.set_level(logging.WARNING)
    game = _game(mode)

    async def broken_countdown():
        raise RuntimeError("countdown failed")

    game._countdown = broken_countdown
    with pytest.raises(RuntimeError, match="countdown failed"):
        await asyncio.wait_for(game.run(), timeout=20)

    _assert_every_player_span_ended_once(exporter, caplog, "error", None)
    finished = [s.name for s in exporter.get_finished_spans()]
    assert finished.count("countdown_phase") == len(SERIALS)
    assert {"players", "game_cycle"} <= set(finished)


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.asyncio
async def test_an_error_after_the_loop_latched_labels_the_spans_with_the_latched_outcome(exporter, caplog):
    caplog.set_level(logging.WARNING)
    game = _game(Games.FightClub)

    async def instant_loop():
        await asyncio.sleep(0.05)

    async def broken_teardown():
        raise RuntimeError("teardown failed")

    game._game_loop = instant_loop
    game._end_shield_feedback = broken_teardown
    with pytest.raises(RuntimeError, match="teardown failed"):
        await asyncio.wait_for(game.run(), timeout=20)

    latched = game._latched_outcome["game.outcome"]
    assert latched != "error"
    _assert_every_player_span_ended_once(exporter, caplog, latched, None)
    assert all(s.status.status_code == StatusCode.ERROR for s in _player_spans(exporter))


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.asyncio
async def test_a_failing_span_end_keeps_the_run_error_and_clears_the_analytics(exporter, caplog):
    game = _game(Games.JoustFFA)

    async def broken_warmup():
        raise RuntimeError("warm-up failed")

    def broken_finalize(_player):
        raise ValueError("finalize failed")

    game._warmup_ema = broken_warmup
    game._finalize_player_health = broken_finalize
    with (
        patch.object(base.metrics, "clear_session_player_analytics") as clear_analytics,
        pytest.raises(RuntimeError, match="warm-up failed"),
    ):
        await asyncio.wait_for(game.run(), timeout=20)

    clear_analytics.assert_called_once()
    assert "Ending the open player spans failed" in caplog.text
    assert {"players", "game_cycle"} <= {s.name for s in exporter.get_finished_spans()}


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.parametrize("mode", RUN_OVERRIDE_MODES + BASE_RUN_MODES, ids=lambda m: m.name)
@pytest.mark.asyncio
async def test_a_natural_end_ends_each_player_span_once_with_the_mode_attributes(exporter, caplog, mode):
    caplog.set_level(logging.WARNING)
    game = _game(mode)

    async def instant_loop():
        await asyncio.sleep(0.05)

    game._game_loop = instant_loop
    await asyncio.wait_for(game.run(), timeout=20)

    spans = _player_spans(exporter)
    assert sorted(s.attributes["player.serial"] for s in spans) == SERIALS
    for span in spans:
        for event in _game_ended(span):
            assert "game.outcome" not in event.attributes, "the mode's own end step closed it"
    assert DOUBLE_END not in caplog.text


@pytest.mark.uc("UC-030", br=["BR-007"])
@pytest.mark.uc("UC-025", flow="main", steps=[5], br=["BR-005"])
@pytest.mark.asyncio
async def test_tournament_natural_end_closes_the_champion_span_with_tournament_ended(exporter, caplog):
    """The end step, not the run-end cleanup, closes the spans: the champion keeps ``tournament_ended``."""
    caplog.set_level(logging.WARNING)
    game = _game(Games.Tournament)
    champion = SERIALS[0]

    async def final_decided_loop():
        await asyncio.sleep(0.05)
        game.players[champion].wins = 2
        game.players[champion].round_number = 2
        assert game._latch_champion(champion)

    game._game_loop = final_decided_loop
    await asyncio.wait_for(game.run(), timeout=20)

    spans = _player_spans(exporter)
    assert sorted(s.attributes["player.serial"] for s in spans) == SERIALS, "one ended span per player"
    for span in spans:
        assert _game_ended(span) == [], "the run-end cleanup must not close a span on a natural end"
        ended = [e for e in span.events if e.name == "tournament_ended"]
        assert len(ended) == 1, span.events
        attrs = dict(ended[0].attributes)
        is_champion = span.attributes["player.serial"] == champion
        assert attrs["is_champion"] is is_champion
        if is_champion:
            assert attrs["wins"] == 2
            assert attrs["final_round"] == 2
    assert DOUBLE_END not in caplog.text
    _assert_no_span_left_open(exporter)


def _recording(game, calls: list[str], attr: str, label: str) -> None:
    """Wrap an async or sync method of ``game`` so each call appends ``label`` before it runs."""
    real = getattr(game, attr)

    if inspect.iscoroutinefunction(real):

        async def wrapper(*args, **kwargs):
            calls.append(label)
            return await real(*args, **kwargs)

    else:

        def wrapper(*args, **kwargs):
            calls.append(label)
            return real(*args, **kwargs)

    setattr(game, attr, wrapper)


@pytest.mark.uc("UC-020", flow="main")
@pytest.mark.usefixtures("exporter")
@pytest.mark.asyncio
async def test_a_streamed_force_end_stops_the_music_before_the_force_end_hook():
    """The streamed finally stops the music first, then calls on_force_end (the base flow does the reverse)."""
    game = _game(Games.FightClub)
    calls: list[str] = []
    _recording(game, calls, "_stop_game_music", "music_stopped")
    _recording(game, calls, "on_force_end", "on_force_end")
    _force_end_after(game, "countdown")
    await asyncio.wait_for(game.run(), timeout=20)

    assert calls == ["music_stopped", "on_force_end"]


@pytest.mark.uc("UC-020", flow="main")
@pytest.mark.asyncio
async def test_the_mode_game_span_is_error_with_the_exception_on_a_run_error_and_ok_on_a_natural_end(exporter):
    failing = _game(Games.FightClub)

    async def broken_warmup():
        raise RuntimeError("warm-up failed")

    failing._warmup_ema = broken_warmup
    with pytest.raises(RuntimeError, match="warm-up failed"):
        await asyncio.wait_for(failing.run(), timeout=20)

    (failed_span,) = [s for s in exporter.get_finished_spans() if s.name == "fight_club_game"]
    assert failed_span.status.status_code == StatusCode.ERROR
    assert "warm-up failed" in (failed_span.status.description or "")
    exception_events = [e for e in failed_span.events if e.name == "exception"]
    assert exception_events, "the exception is recorded on the mode span"
    assert {e.attributes["exception.message"] for e in exception_events} == {"warm-up failed"}

    exporter.clear()
    natural = _game(Games.FightClub)

    async def instant_loop():
        await asyncio.sleep(0.05)

    natural._game_loop = instant_loop
    await asyncio.wait_for(natural.run(), timeout=20)

    (ok_span,) = [s for s in exporter.get_finished_spans() if s.name == "fight_club_game"]
    assert ok_span.status.status_code == StatusCode.OK
    assert [e for e in ok_span.events if e.name == "exception"] == []


@pytest.mark.uc("UC-030", flow="main")
@pytest.mark.usefixtures("exporter")
@pytest.mark.asyncio
async def test_tournament_releases_the_stream_after_the_winner_effects_not_only_in_the_cleanup():
    game = _game(Games.Tournament)
    calls: list[str] = []
    _recording(game, calls, "_end_game_impl", "end_game_impl")
    _recording(game, calls, "_release_gameplay_stream", "release")
    _recording(game, calls, "_cleanup_after_run", "cleanup")

    async def instant_loop():
        await asyncio.sleep(0.05)

    game._game_loop = instant_loop
    await asyncio.wait_for(game.run(), timeout=20)

    assert calls[:3] == ["end_game_impl", "release", "cleanup"]
