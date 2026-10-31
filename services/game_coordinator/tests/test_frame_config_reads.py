"""Per-frame cost of the game loop (#2361).

The runtime config is event-refreshed (docs/specs/flags.md): a frame reads it
once and every player of the frame uses that snapshot, instead of two reads per
player (health and analytics). The ``accel_distribution`` label lookup runs once
per game, not once per player per frame.
"""

import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

test_dir = Path(__file__).parent
sys.path.insert(0, str(test_dir.parent.parent.parent))
sys.path.insert(0, str(test_dir))

from conftest import MockControllerManagerService, MockGameplayStream, async_noop

from proto import controller_manager_pb2
from services.game_coordinator import metrics
from services.game_coordinator.games.analytics import PlayerAnalytics
from services.game_coordinator.games.base import Player
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.runtime_config import AnalyticsConfig, GamePerformanceConfig, RuntimeConfigManager

SERIALS = ["AA:00:01", "AA:00:02", "AA:00:03"]
FRAMES = 5


def _add_players(game) -> None:
    for serial in SERIALS:
        player = Player(serial=serial)
        player.analytics = PlayerAnalytics(serial=serial, game_start_time=0.0)
        game.players[serial] = player


def _make_ffa() -> FFAGame:
    game = FFAGame(
        controller_manager_client=MockControllerManagerService(num_controllers=len(SERIALS)),
        event_publisher=async_noop,
        audio_client=None,
        game_id="frame_reads",
    )
    game.gameplay_stream = MockGameplayStream()
    _add_players(game)
    return game


def _frame() -> controller_manager_pb2.GameplayDataUpdate:
    # A still controller (1 g): no warning, no death.
    return controller_manager_pb2.GameplayDataUpdate(
        controllers=[
            controller_manager_pb2.GameplayData(
                serial=serial,
                accel=controller_manager_pb2.Vector3(x=0, y=0, z=1.0),
                gyro=controller_manager_pb2.Vector3(x=0, y=0, z=0),
            )
            for serial in SERIALS
        ]
    )


def _config_manager():
    manager = create_autospec(RuntimeConfigManager, instance=True)
    manager.get_config.return_value = GamePerformanceConfig()
    return manager


@pytest.mark.uc("UC-030", flow="main")
@pytest.mark.asyncio
async def test_config_read_once_per_frame():
    """Every player of a frame shares one config read (was two reads per player)."""
    game = _make_ffa()
    manager = _config_manager()
    with patch("services.game_coordinator.games.base.get_config_manager", return_value=manager):
        for _ in range(FRAMES):
            assert await game._process_gameplay_update(_frame()) is False

    assert manager.get_config.call_count == FRAMES
    # Analytics ran on the shared snapshot for every player of every frame.
    assert all(p.analytics.sample_count == FRAMES for p in game.players.values())
    # Outside a frame the live snapshot is read again.
    assert game._frame_config is None


@pytest.mark.uc("UC-030", flow="main")
@pytest.mark.asyncio
async def test_config_refresh_applies_from_the_next_frame():
    """A refreshed snapshot is picked up by the next frame: the value stays live per frame."""
    game = _make_ffa()
    manager = _config_manager()
    disabled = replace(GamePerformanceConfig(), analytics=AnalyticsConfig(enabled=False))
    manager.get_config.side_effect = [GamePerformanceConfig(), disabled]
    with patch("services.game_coordinator.games.base.get_config_manager", return_value=manager):
        await game._process_gameplay_update(_frame())
        await game._process_gameplay_update(_frame())

    assert all(p.analytics.sample_count == 1 for p in game.players.values())


@pytest.mark.uc("UC-030", flow="main")
@pytest.mark.asyncio
async def test_accel_distribution_labels_bound_once_per_game():
    """The histogram child is bound once; every sample is still observed."""
    game = _make_ffa()
    histogram = create_autospec(metrics.accel_distribution, instance=True)
    with (
        patch("services.game_coordinator.games.base.get_config_manager", return_value=_config_manager()),
        patch.object(metrics, "accel_distribution", histogram),
    ):
        for _ in range(FRAMES):
            await game._process_gameplay_update(_frame())

    histogram.labels.assert_called_once_with(game_mode="FFA")
    assert histogram.labels.return_value.observe.call_count == FRAMES * len(SERIALS)


@pytest.mark.uc("UC-030", flow="main")
def test_overlapping_frame_scopes_never_pin_a_closed_snapshot():
    """Scopes that exit out of order leave the open one's snapshot, then the live read."""
    game = _make_ffa()
    first, second = GamePerformanceConfig(), GamePerformanceConfig()
    manager = _config_manager()
    manager.get_config.side_effect = [first, second]
    with patch("services.game_coordinator.games.base.get_config_manager", return_value=manager):
        scope_a = game._frame_config_scope()
        scope_b = game._frame_config_scope()
        scope_a.__enter__()
        scope_b.__enter__()
        assert game._runtime_config() is second
        scope_a.__exit__(None, None, None)  # A closes first: B's snapshot stays
        assert game._runtime_config() is second
        scope_b.__exit__(None, None, None)

    assert game._frame_config is None
    assert game._frame_configs == ()
