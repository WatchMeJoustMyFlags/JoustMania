"""Retire vs. game-thread teardown: no per-game series survives retire (#1311).

Retire (``GameSession.clear_metrics``, called by the servicer's natural-end
sweep and by force-end) can run before the game thread's ``finally:`` in
``_run_game_loop_async``. Before #1311 that ``finally:`` then re-set
``game_active`` / ``game_active_players`` to 0 and wrote the final
``game_duration_seconds``, re-creating series nothing removes again: one leaked
set per affected game, unbounded with shadow churn.

The race is reproduced deterministically: the fake game's ``run()`` retires the
session itself (as the sweep does once the game published its ending event)
before it returns, so retire always wins against the ``finally:``.

Also covers #1471 item 1: the loop-health gauges are per game (game_kind,
game_id), so a concurrent real and shadow loop never overwrite each other, and
retire removes them with the other game-state gauges.
"""

import contextlib
import sys
import time
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest
from opentelemetry import trace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from lib.types import GameEvent
from proto import game_coordinator_pb2
from services.game_coordinator import game_session, metrics
from services.game_coordinator.event_bus import EventBus
from services.game_coordinator.game_session import GAME_KIND_PRIMARY, GAME_KIND_SHADOW, GameSession
from services.game_coordinator.games.base import BaseGameMode
from services.game_coordinator.grpc_clients import GrpcClientManager

GAME_ID = "game_race01"

# Every series the session writes with this game's labels.
SESSION_GAUGES = (
    metrics.active_game,
    metrics.active_players,
    metrics.game_duration_seconds,
    metrics.current_game_mode,
    metrics.game_trace_correlation,
)
LOOP_HEALTH_GAUGES = (
    metrics.game_loop_jitter_ms,
    metrics.game_loop_frame_consistency_percent,
)


@pytest.fixture
def live_gauges(monkeypatch):
    """Make the real gauges record series (metrics are off in tests)."""
    for gauge in (*SESSION_GAUGES, *metrics.GAME_STATE_GAUGES):
        monkeypatch.setattr(gauge, "_instrument_initialized", True)
        monkeypatch.setattr(gauge, "_values", {})
        monkeypatch.setattr(gauge, "_metrics", {})


@pytest.fixture
def completed_counter():
    with patch.object(metrics, "games_completed_total", autospec=True) as counter:
        yield counter


def _series_for(gauge, game_id: str) -> list[dict]:
    return [dict(key) for key in gauge._values if dict(key).get("game_id") == game_id]


def _make_session(game_kind: str = GAME_KIND_SHADOW, game_id: str = GAME_ID) -> GameSession:
    event_bus = create_autospec(EventBus, instance=True)
    session = GameSession(
        game_id=game_id,
        game_name="FFA",
        players=[game_coordinator_pb2.Player(serial="p1"), game_coordinator_pb2.Player(serial="p2")],
        game_config=game_coordinator_pb2.StartGameConfig(sensitivity=2),
        event_bus=event_bus,
        game_kind=game_kind,
        experiment_id="exp_1" if game_kind == GAME_KIND_SHADOW else "",
        arm="control" if game_kind == GAME_KIND_SHADOW else "",
    )
    clients = create_autospec(GrpcClientManager, instance=True)
    clients.is_connected = True
    session.clients = clients
    # What the servicer sets when it admits the game.
    labels = {"game_kind": game_kind, "game_id": game_id, "experiment_id": session.experiment_id, "arm": session.arm}
    metrics.active_game.labels(**labels).set(1)
    metrics.active_players.labels(**labels).set(2)
    session.game_running = True
    session.game_start_time = time.time() - 5.0
    return session


def _sampled_span_cm():
    """A started-span context manager whose span has a valid (sampled) context."""
    span = trace.NonRecordingSpan(
        trace.SpanContext(
            trace_id=0x4BF92F3577B34DA6A3CE929D0E0E4736,
            span_id=0x051581BF3CB55C13,
            is_remote=False,
            trace_flags=trace.TraceFlags(trace.TraceFlags.SAMPLED),
        )
    )
    return contextlib.nullcontext(span)


def _game_that_retires_before_teardown(session: GameSession):
    """A game whose run() ends the game and lets retire win before the loop's finally."""
    game = create_autospec(BaseGameMode, instance=True)
    game.get_game_name.return_value = "FFA"
    game.game_outcome.return_value = "winner"

    async def run():
        session.on_event_state_sync(GameEvent.GAME_STARTED)
        session._set_duration_gauge()  # the live ticker has written at least once
        session.publish_gameplay_phase_span_id("00f067aa0ba902b7")
        # The game published its ending event (event-sync sets ENDED), and the
        # natural-end sweep retires the session before the loop's finally ran.
        session.on_event_state_sync(GameEvent.GAME_ENDED)
        assert session.game_state == game_coordinator_pb2.GameState.ENDED
        session.clear_metrics()

    game.run.side_effect = run
    return game


async def _run_loop(session: GameSession, game) -> None:
    with (
        patch("services.game_coordinator.game_session.GameFactory", autospec=True) as factory,
        patch("services.game_coordinator.game_session.tracer", autospec=True) as tracer,
    ):
        factory.create_game.return_value = game
        tracer.start_as_current_span.return_value = _sampled_span_cm()
        await session._run_game_loop_async()


class TestRetireBeforeTeardown:
    @pytest.mark.uc("UC-041", flow="main", steps=[5, 7])
    @pytest.mark.parametrize("game_kind", [GAME_KIND_PRIMARY, GAME_KIND_SHADOW])
    @pytest.mark.asyncio
    async def test_no_series_survives_when_retire_wins(self, live_gauges, completed_counter, game_kind):
        """#1311: the loop's finally after retire re-creates none of the game's series."""
        session = _make_session(game_kind=game_kind)
        game = _game_that_retires_before_teardown(session)

        await _run_loop(session, game)

        game.run.assert_awaited_once()
        for gauge in SESSION_GAUGES:
            assert _series_for(gauge, GAME_ID) == [], gauge.name
        assert session.game_state == game_coordinator_pb2.GameState.ENDED

    @pytest.mark.uc("UC-041", flow="main", steps=[7])
    @pytest.mark.asyncio
    async def test_outcome_counter_still_counts_once_when_retire_wins(self, live_gauges, completed_counter):
        """#1311: the cumulative games_completed_total is not a retired series; the
        game is still counted exactly once with its outcome."""
        session = _make_session()
        game = _game_that_retires_before_teardown(session)

        await _run_loop(session, game)

        completed_counter.labels.assert_called_once_with(mode="FFA", game_kind=GAME_KIND_SHADOW, outcome="winner")
        completed_counter.labels.return_value.inc.assert_called_once_with()

    @pytest.mark.uc("UC-041", flow="main", steps=[7])
    @pytest.mark.asyncio
    async def test_teardown_before_retire_still_zeroes_then_retire_removes(self, live_gauges, completed_counter):
        """The normal order is unchanged: teardown writes 0 / the final duration,
        then retire removes every series."""
        session = _make_session()
        game = create_autospec(BaseGameMode, instance=True)
        game.get_game_name.return_value = "FFA"
        game.game_outcome.return_value = "tie"

        await _run_loop(session, game)

        active = {dict(k)["game_id"]: v for k, v in metrics.active_game._values.items()}
        assert active == {GAME_ID: 0}
        assert _series_for(metrics.game_duration_seconds, GAME_ID), "final duration is written before retire"

        session.clear_metrics()

        for gauge in SESSION_GAUGES:
            assert _series_for(gauge, GAME_ID) == [], gauge.name


REMOVAL_DELAY_S = 0.2


def _value_for(gauge, game_id: str):
    values = [v for k, v in gauge._values.items() if dict(k).get("game_id") == game_id]
    return values[0] if values else None


def _wait_until_removed(gauge, game_id: str, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while _series_for(gauge, game_id) and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.fixture
def delayed_removal():
    """Retire with a real (short) removal delay, as when metrics are exported."""
    with patch.object(game_session, "retire_removal_delay_s", autospec=True, return_value=REMOVAL_DELAY_S):
        yield


class TestRetireExportsTheEnd:
    """#1311 review: the agent detects a game's end only from game_active 1 -> 0,
    so retire must export a 0 before the series is removed."""

    @pytest.mark.uc("UC-041", flow="main", steps=[7])
    @pytest.mark.asyncio
    async def test_retire_wins_still_exports_zero_then_removes(self, live_gauges, completed_counter, delayed_removal):
        session = _make_session()
        game = _game_that_retires_before_teardown(session)

        await _run_loop(session, game)

        # The loop's teardown is done and retire won: the series still read 0.
        assert _value_for(metrics.active_game, GAME_ID) == 0
        assert _value_for(metrics.active_players, GAME_ID) == 0

        for gauge in SESSION_GAUGES:
            _wait_until_removed(gauge, GAME_ID)
            assert _series_for(gauge, GAME_ID) == [], gauge.name

    @pytest.mark.uc("UC-041", flow="main", steps=[7])
    @pytest.mark.asyncio
    async def test_force_end_then_retire_exports_zero_then_removes(self, live_gauges, delayed_removal):
        """Force-end + retire while the game thread has not torn down: game_active
        goes 1 -> 0 at retire, without blocking on the removal delay."""
        session = _make_session()
        session.game_state = game_coordinator_pb2.GameState.RUNNING
        session.current_game = create_autospec(BaseGameMode, instance=True)
        assert _value_for(metrics.active_game, GAME_ID) == 1

        ok, _ = await session.force_end("test")
        started = time.monotonic()
        session.clear_metrics()  # what the servicer's _retire_session does next
        assert time.monotonic() - started < REMOVAL_DELAY_S, "retire must not wait for the removal"

        assert ok
        assert _value_for(metrics.active_game, GAME_ID) == 0
        assert _value_for(metrics.active_players, GAME_ID) == 0
        _wait_until_removed(metrics.active_game, GAME_ID)
        assert _series_for(metrics.active_game, GAME_ID) == []
        assert _series_for(metrics.active_players, GAME_ID) == []

    @pytest.mark.uc("UC-041", flow="main", steps=[7])
    def test_writes_during_removal_delay_are_dropped(self, live_gauges, delayed_removal):
        """Between retire's zeroing and the removal, session writes stay no-ops:
        game_active is not set back to 1 and no other series is re-created."""
        session = _make_session()
        session.game_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        session.game_trace_span_id = "051581bf3cb55c13"
        session.clear_metrics()
        assert _value_for(metrics.active_game, GAME_ID) == 0  # removal still pending

        session._set_duration_gauge()
        session._set_gauges_unless_retired(session._set_trace_correlation)
        session.publish_game_mode("FFA")

        assert _value_for(metrics.active_game, GAME_ID) == 0
        for gauge in (metrics.game_duration_seconds, metrics.current_game_mode, metrics.game_trace_correlation):
            assert _series_for(gauge, GAME_ID) == [], gauge.name
        for gauge in SESSION_GAUGES:
            _wait_until_removed(gauge, GAME_ID)
            assert _series_for(gauge, GAME_ID) == [], gauge.name

    @pytest.mark.uc("UC-041", flow="main", steps=[7])
    def test_removal_delay_is_three_export_intervals(self):
        with patch.object(game_session, "get_export_interval_ms", autospec=True, return_value=1000):
            assert game_session.retire_removal_delay_s() == 3.0
        with patch.object(game_session, "get_export_interval_ms", autospec=True, return_value=None):
            assert game_session.retire_removal_delay_s() == 0.0


class TestWritesAfterRetire:
    @pytest.mark.uc("UC-041", flow="main", steps=[7])
    def test_session_writes_after_retire_are_dropped(self, live_gauges):
        """Each per-game write path of the session is a no-op once retired (#1311)."""
        session = _make_session()
        session.game_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        session.game_trace_span_id = "051581bf3cb55c13"
        session.clear_metrics()

        session._set_duration_gauge()
        session._set_gauges_unless_retired(session._zero_lifecycle_gauges)
        session._set_gauges_unless_retired(session._set_trace_correlation)
        session.publish_game_mode("FFA")
        session.publish_gameplay_phase_span_id("00f067aa0ba902b7")
        session.publish_game_cycle_span_id("00f067aa0ba902b8")

        for gauge in SESSION_GAUGES:
            assert _series_for(gauge, GAME_ID) == [], gauge.name
        assert session.game_mode == ""

    @pytest.mark.uc("UC-041", flow="main", steps=[7])
    def test_clear_metrics_twice_is_safe(self, live_gauges):
        session = _make_session()
        session.clear_metrics()
        session.clear_metrics()
        assert _series_for(metrics.active_game, GAME_ID) == []


class TestLoopHealthGaugesPerGame:
    """#1471 item 1: jitter and frame consistency are per game, removed at retire."""

    @staticmethod
    def _game(game_id: str, game_kind: str):
        game = create_autospec(BaseGameMode, instance=True)
        game.game_id = game_id
        game.game_kind = game_kind
        game._game_state_labels.side_effect = lambda: {"game_kind": game_kind, "game_id": game_id}
        game.get_game_name.return_value = "FFA"
        return game

    @staticmethod
    def _emit(game, frame_times: list[float]) -> None:
        """Run the real frame-metrics update for ``game`` at the 10th iteration."""
        BaseGameMode._update_frame_metrics(
            game,
            iteration_latency_ms=frame_times[-1],
            target_frame_time_ms=16.7,
            recent_frame_times=list(frame_times[:-1]),
            frames_on_target=8,
            loop_iterations=10,
            loop_start_time=time.time() - 1.0,
        )

    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    def test_loop_health_gauges_declare_game_kind_and_game_id(self):
        for gauge in LOOP_HEALTH_GAUGES:
            assert gauge.labelnames == ["game_kind", "game_id"], gauge.name
            assert gauge in metrics.GAME_STATE_GAUGES, gauge.name

    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    def test_concurrent_real_and_shadow_loops_do_not_overwrite(self, live_gauges):
        real = self._game("game_real", GAME_KIND_PRIMARY)
        shadow = self._game("game_shadow", GAME_KIND_SHADOW)

        self._emit(real, [16.0, 17.0, 16.5])
        self._emit(shadow, [5.0, 40.0, 90.0])

        for gauge in (*LOOP_HEALTH_GAUGES, metrics.actual_update_frequency_hz):
            by_game = {dict(k)["game_id"]: (dict(k)["game_kind"], v) for k, v in gauge._values.items()}
            assert set(by_game) == {"game_real", "game_shadow"}, gauge.name
            assert by_game["game_real"][0] == GAME_KIND_PRIMARY
            assert by_game["game_shadow"][0] == GAME_KIND_SHADOW
        jitter = {dict(k)["game_id"]: v for k, v in metrics.game_loop_jitter_ms._values.items()}
        assert jitter["game_shadow"] > jitter["game_real"]

    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    def test_retire_removes_only_that_games_loop_health_series(self, live_gauges):
        real = self._game("game_real", GAME_KIND_PRIMARY)
        shadow = self._game("game_shadow", GAME_KIND_SHADOW)
        self._emit(real, [16.0, 17.0, 16.5])
        self._emit(shadow, [5.0, 40.0, 90.0])

        _make_session(game_kind=GAME_KIND_SHADOW, game_id="game_shadow").clear_metrics()

        for gauge in LOOP_HEALTH_GAUGES:
            assert {dict(k)["game_id"] for k in gauge._values} == {"game_real"}, gauge.name
