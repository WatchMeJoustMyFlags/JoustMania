"""GameInitFlags: the validated, immutable snapshot of the init-latched game flags (#2359)."""

import dataclasses
import sys
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from conftest import EventCollector  # noqa: E402

from services.game_coordinator.games import base
from services.game_coordinator.games.base import DEATH_GRACE_PERIOD, GAME_VOLUME, SLOW_MAX
from services.game_coordinator.games.ffa import FFAGame
from services.game_coordinator.games.flag_snapshot import GameInitFlags, MusicWindows
from services.game_coordinator.runtime_config import RuntimeConfigManager


def _make_ffa():
    return FFAGame(controller_manager_client=None, event_publisher=EventCollector().publish, sensitivity=2)


def _raw(**overrides):
    raw = {
        "thresholds": {},
        "ema_weight": 4.0,
        "death_grace_period_seconds": DEATH_GRACE_PERIOD,
        "death_grace_default": DEATH_GRACE_PERIOD,
        "windows": {},
        "game_volume": GAME_VOLUME,
        "countdown_music_volume": 0.15,
    }
    raw.update(overrides)
    return raw


def test_snapshot_is_frozen():
    snapshot = GameInitFlags.from_raw(**_raw())
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.slow_max = (1, 2, 3, 4, 5)
    assert isinstance(snapshot.slow_max, tuple)


def test_malformed_raw_values_give_the_defaults():
    snapshot = GameInitFlags.from_raw(
        **_raw(
            thresholds="junk",
            death_grace_period_seconds=-1.0,
            windows=[1],
        )
    )
    assert snapshot.slow_max == tuple(SLOW_MAX)
    assert snapshot.death_grace_period_seconds == DEATH_GRACE_PERIOD
    assert snapshot.music_windows == MusicWindows.defaults()


def test_game_attributes_start_from_one_snapshot_read_once():
    """Each init flag is read exactly once through the patchable readers on base."""
    manager = create_autospec(RuntimeConfigManager, instance=True)
    manager.read_ema_weight.return_value = 3.0
    with (
        patch.object(base, "get_config_manager", autospec=True, return_value=manager) as get_manager,
        patch.object(base, "_read_volume_flag", return_value=0.5) as volumes,
        patch.object(base, "read_object_flag", return_value={}) as objects,
        patch.object(base, "read_float_flag", return_value=0.25) as floats,
    ):
        game = _make_ffa()
    assert [c.args[1] for c in objects.call_args_list] == ["thresholds", "windows"]
    assert [c.args[1] for c in floats.call_args_list] == ["death_grace_period_seconds"]
    assert get_manager.call_count == 1
    manager.read_ema_weight.assert_called_once_with()
    assert [c.args[0] for c in volumes.call_args_list] == ["audio_volume.game", "audio_volume.countdown_music"]
    assert game.death_grace_period == game.init_flags.death_grace_period_seconds == 0.25
    assert game.music_windows is game.init_music_windows is game.init_flags.music_windows
