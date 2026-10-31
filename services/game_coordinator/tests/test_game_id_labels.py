"""
Tests for the additive ``game_id`` label on live-session and per-player metrics,
plus ``game.id`` / ``game.kind`` on the player_lifecycle span (#845).

These guarantee the producer side of per-game agent partitioning: every live
signal the agent routes on is stamped with the emitting session's game_id, so
concurrent games never blur into one partition.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService, MockGameplayStream

from proto import game_coordinator_pb2
from services.game_coordinator import metrics
from services.game_coordinator.games.ffa import FFAGame

GAME_ID = "test_labels_001"


@pytest_asyncio.fixture
async def ffa_game():
    """FFA game with 4 initialized players and a known game_id."""
    mock_cm = MockControllerManagerService(num_controllers=4, death_schedule={}, max_duration=10.0)
    collector = EventCollector()
    game = FFAGame(
        controller_manager_client=mock_cm,
        event_publisher=collector.publish,
        audio_client=None,
        game_id=GAME_ID,
    )
    await game._initialize_players_impl(mock_cm.controllers)
    game.gameplay_stream = MockGameplayStream()
    return game


class TestPerPlayerMetricLabels:
    @pytest.mark.asyncio
    async def test_kill_player_stamps_alive_with_game_id(self, ffa_game):
        game = ffa_game
        serial = next(iter(game.players))

        with patch("services.game_coordinator.metrics.player_alive") as mock_alive:
            await game._kill_player(serial, accel_mag=5.0)

        mock_alive.labels.assert_called_with(serial=serial, game_id=GAME_ID)
        mock_alive.labels.return_value.set.assert_called_with(0)

    @pytest.mark.asyncio
    async def test_initialize_marks_alive_with_game_id(self):
        mock_cm = MockControllerManagerService(num_controllers=2, death_schedule={}, max_duration=10.0)
        collector = EventCollector()
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=collector.publish,
            audio_client=None,
            game_id=GAME_ID,
            initial_players=[game_coordinator_pb2.Player(serial="p1"), game_coordinator_pb2.Player(serial="p2")],
        )

        with patch("services.game_coordinator.metrics.player_alive") as mock_alive:
            # The StartGame-RPC init path sets player_alive=1 per player.
            await game._initialize_players()

        assert mock_alive.labels.call_args_list, "player_alive never labeled during init"
        for call in mock_alive.labels.call_args_list:
            assert call.kwargs["game_id"] == GAME_ID
        mock_alive.labels.return_value.set.assert_called_with(1)


class TestPlayersAliveAggregateIsPrimaryOnly:
    """The unlabeled ``players_alive`` aggregate gauge (the live-dashboard count)
    is written by the PRIMARY session only. With concurrent shadow games (#1018)
    a headless shadow must not stomp the real game's count via this single
    last-writer-wins series."""

    @pytest.mark.asyncio
    async def test_primary_kill_writes_players_alive(self, ffa_game):
        game = ffa_game  # default game_kind == "primary"
        game.running = True  # the aggregate is only written while the game runs (#1943)
        serial = next(iter(game.players))

        with patch("services.game_coordinator.metrics.players_alive") as mock_agg:
            await game._kill_player(serial, accel_mag=5.0)

        mock_agg.set.assert_called()  # primary updates the aggregate

    @pytest.mark.asyncio
    async def test_shadow_kill_does_not_write_players_alive(self, ffa_game):
        game = ffa_game
        game.game_kind = "shadow"
        game.running = True
        serial = next(iter(game.players))

        with patch("services.game_coordinator.metrics.players_alive") as mock_agg:
            await game._kill_player(serial, accel_mag=5.0)

        mock_agg.set.assert_not_called()  # shadow must NOT touch the aggregate

    @pytest.mark.asyncio
    async def test_shadow_initialize_does_not_write_players_alive(self):
        mock_cm = MockControllerManagerService(num_controllers=2, death_schedule={}, max_duration=10.0)
        collector = EventCollector()
        game = FFAGame(
            controller_manager_client=mock_cm,
            event_publisher=collector.publish,
            audio_client=None,
            game_id=GAME_ID,
            initial_players=[game_coordinator_pb2.Player(serial="p1"), game_coordinator_pb2.Player(serial="p2")],
        )
        game.game_kind = "shadow"

        with patch("services.game_coordinator.metrics.players_alive") as mock_agg:
            await game._initialize_players()

        mock_agg.set.assert_not_called()


class TestLifecycleSpanGameIdentity:
    def test_lifecycle_span_carries_game_id_and_kind(self, ffa_game):
        game = ffa_game
        game.game_kind = "shadow"
        serial = next(iter(game.players))

        with patch("services.game_coordinator.games.base.tracer") as mock_tracer:
            game._create_player_lifecycle_span(serial)

        _, kwargs = mock_tracer.start_span.call_args
        attrs = kwargs["attributes"]
        assert attrs["game.id"] == GAME_ID
        assert attrs["game.kind"] == "shadow"
        assert attrs["player.serial"] == serial


class TestMetricDeclarationsCarryGameId:
    """The label set on the gauge/counter declarations must include game_id so
    the agent's extractors can read it (contract for PR A)."""

    def test_live_session_metrics_have_game_id(self):
        for metric in (metrics.active_game, metrics.active_players, metrics.game_duration_seconds):
            assert "game_id" in metric.labelnames

    def test_per_player_metrics_have_game_id(self):
        for metric in (
            metrics.player_alive,
            metrics.player_accel_magnitude,
            metrics.player_movement_zone,
            metrics.player_playstyle,
            metrics.player_movement_variance,
            metrics.player_movement_variance_aggregate,
            metrics.player_skill_level,
            metrics.player_peak_accel,
            metrics.player_elimination_order,
        ):
            assert "game_id" in metric.labelnames

    def test_deaths_total_is_an_aggregate_counter(self):
        """A cumulative counter must not carry serial/game_id (unbounded series, #1334)."""
        assert set(metrics.player_deaths_total.labelnames) == {"mode", "game_kind"}


class TestCleanupRemovesGameIdScopedSeries:
    def test_clear_player_analytics_removes_serial_and_game_id_tuple(self):
        with (
            patch("services.game_coordinator.metrics.player_alive") as mock_alive,
            patch("services.game_coordinator.metrics.player_accel_magnitude") as mock_accel,
            patch("services.game_coordinator.metrics.player_skill_level") as mock_skill,
            patch("services.game_coordinator.metrics.player_movement_variance_aggregate") as mock_var_agg,
        ):
            metrics.clear_player_analytics("AA:BB:CC", game_id=GAME_ID)

        mock_alive.remove.assert_called_once_with("AA:BB:CC", GAME_ID)
        mock_accel.remove.assert_called_once_with("AA:BB:CC", GAME_ID)
        mock_skill.remove.assert_called_once_with("AA:BB:CC", GAME_ID)
        mock_var_agg.remove.assert_called_once_with("AA:BB:CC", GAME_ID)


# ---------------------------------------------------------------------------
# Game-wide state gauges carry game_kind + game_id (FR-041, UC-041 step 5).
#
# music_tempo, game_sensitivity, effective_*_threshold, actual_update_frequency_hz
# and current_game_mode used to be single unlabeled series: a concurrent real game
# and shadow game overwrote each other, and the agent attributed the last write to
# whichever game it had selected. Each game now writes its own series and removes
# it when it ends.
# ---------------------------------------------------------------------------

GAME_STATE_GAUGES = (
    metrics.music_tempo,
    metrics.game_sensitivity,
    metrics.effective_warning_threshold,
    metrics.effective_death_threshold,
    metrics.actual_update_frequency_hz,
    metrics.game_loop_frame_consistency_percent,
    metrics.game_loop_jitter_ms,
)


@pytest.fixture
def live_gauges(monkeypatch):
    """Make the real game-state gauges record values (metrics are off in tests)."""
    for gauge in (*GAME_STATE_GAUGES, metrics.current_game_mode):
        monkeypatch.setattr(gauge, "_instrument_initialized", True)
        monkeypatch.setattr(gauge, "_values", {})
        monkeypatch.setattr(gauge, "_metrics", {})


def _series_by_game(gauge) -> dict[str, tuple[str, float]]:
    """Map game_id -> (game_kind, value) for every live series of ``gauge``."""
    out = {}
    for key, value in gauge._values.items():
        labels = dict(key)
        out[labels["game_id"]] = (labels["game_kind"], value)
    return out


def _make_ffa(game_id: str, game_kind: str) -> FFAGame:
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=2, death_schedule={}, max_duration=10.0),
        event_publisher=EventCollector().publish,
        audio_client=None,
        game_id=game_id,
    )
    game.game_kind = game_kind
    return game


class TestGameStateGaugesPerGame:
    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    def test_game_state_gauges_declare_game_kind_and_game_id(self):
        for gauge in GAME_STATE_GAUGES:
            assert gauge.labelnames == ["game_kind", "game_id"], gauge.name
        assert metrics.current_game_mode.labelnames == ["mode", "game_kind", "game_id"]

    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    def test_concurrent_games_do_not_overwrite_each_other(self, live_gauges):
        real = _make_ffa("game_real", "primary")
        shadow = _make_ffa("game_shadow", "shadow")
        real.music_speed = 1.0
        shadow.music_speed = 1.3

        for game in (real, shadow):
            game._emit_sensitivity_metric()
            game._emit_music_tempo_metric()
            game._emit_threshold_metrics()

        tempo = _series_by_game(metrics.music_tempo)
        assert tempo == {"game_real": ("primary", 1.0), "game_shadow": ("shadow", 1.3)}
        death = _series_by_game(metrics.effective_death_threshold)
        assert set(death) == {"game_real", "game_shadow"}
        # Slow vs fast tempo interpolate to different thresholds; both survive.
        assert death["game_real"][1] != death["game_shadow"][1]
        assert set(_series_by_game(metrics.effective_warning_threshold)) == {"game_real", "game_shadow"}
        assert set(_series_by_game(metrics.game_sensitivity)) == {"game_real", "game_shadow"}

    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    def test_game_end_removes_only_that_games_series(self, live_gauges):
        real = _make_ffa("game_real", "primary")
        shadow = _make_ffa("game_shadow", "shadow")
        for game in (real, shadow):
            game._emit_sensitivity_metric()
            game._emit_music_tempo_metric()
            game._emit_threshold_metrics()
            labels = {"game_kind": game.game_kind, "game_id": game.game_id}
            metrics.actual_update_frequency_hz.labels(**labels).set(30)
            metrics.game_loop_frame_consistency_percent.labels(**labels).set(98.0)
            metrics.game_loop_jitter_ms.labels(**labels).set(1.5)

        # The shadow ends first: its series go, the real game's stay.
        metrics.clear_session_player_analytics(list(shadow.players), game_id="game_shadow", game_kind="shadow")
        for gauge in GAME_STATE_GAUGES:
            assert set(_series_by_game(gauge)) == {"game_real"}, gauge.name

        metrics.clear_session_player_analytics(list(real.players), game_id="game_real", game_kind="primary")
        for gauge in GAME_STATE_GAUGES:
            assert _series_by_game(gauge) == {}, gauge.name

    @pytest.mark.uc("UC-041", flow="main", steps=[5])
    @pytest.mark.asyncio
    async def test_game_that_raises_removes_its_own_series(self, live_gauges):
        """run()'s finally removes this game's game-state series even on error,
        and leaves a concurrent game's series alone."""
        other = _make_ffa("game_other", "shadow")
        other._emit_sensitivity_metric()
        other._emit_music_tempo_metric()

        game = FFAGame(
            controller_manager_client=MockControllerManagerService(
                num_controllers=2, death_schedule={}, max_duration=10.0
            ),
            event_publisher=EventCollector().publish,
            audio_client=None,
            game_id="game_boom",
            initial_players=[game_coordinator_pb2.Player(serial="p1"), game_coordinator_pb2.Player(serial="p2")],
        )
        game.music_speed = 1.0
        game._emit_music_tempo_metric()

        sensitivity_seen = []

        async def boom():
            sensitivity_seen.extend(_series_by_game(metrics.game_sensitivity))
            raise RuntimeError("boom")

        # Fails right after the init phase emitted game_sensitivity.
        game._initialize_players = boom
        with pytest.raises(RuntimeError, match="boom"):
            await game.run()

        assert "game_boom" in sensitivity_seen  # was set before the failure
        for gauge in (metrics.music_tempo, metrics.game_sensitivity):
            assert set(_series_by_game(gauge)) == {"game_other"}, gauge.name
