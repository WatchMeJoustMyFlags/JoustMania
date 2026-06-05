"""
Tests for #766 F3: music schedule windows promoted to game flags + pacing presets.

Covers, per the issue's consistency rules:
- Characterization: the ``game.windows`` default variant reproduces the current
  hardcoded scheduling exactly (the music loop draws delays within the same
  ranges as the module constants).
- Malformed flag values fall back (as a whole set) to the module constants.
- min <= max ordering: any inverted window pair reverts the whole set.
- Preset variants (calm/default/frantic) in game.json are well-formed: each
  parses through ``resolve_music_windows`` without falling back to defaults.
- Init-frozen: the windows are read once at __init__; the resolved
  ``self.music_windows`` is the F6-swappable seam.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector

from services.game_coordinator.games.base import (
    END_MAX_MUSIC_FAST_TIME,
    END_MAX_MUSIC_SLOW_TIME,
    END_MIN_MUSIC_FAST_TIME,
    END_MIN_MUSIC_SLOW_TIME,
    MAX_MUSIC_FAST_TIME,
    MAX_MUSIC_SLOW_TIME,
    MIN_MUSIC_FAST_TIME,
    MIN_MUSIC_SLOW_TIME,
    MusicWindows,
    resolve_music_windows,
)
from services.game_coordinator.games.ffa import FFAGame

GAME_JSON = project_root / "services" / "flagd" / "game.json"


def _make_ffa():
    return FFAGame(
        controller_manager_client=None,
        event_publisher=EventCollector().publish,
        sensitivity=2,
    )


def _default_flag_dict() -> dict:
    """The exact game.windows default object (snake_case after the constants)."""
    return {
        "min_music_fast_time": MIN_MUSIC_FAST_TIME,
        "max_music_fast_time": MAX_MUSIC_FAST_TIME,
        "min_music_slow_time": MIN_MUSIC_SLOW_TIME,
        "max_music_slow_time": MAX_MUSIC_SLOW_TIME,
        "end_min_music_fast_time": END_MIN_MUSIC_FAST_TIME,
        "end_max_music_fast_time": END_MAX_MUSIC_FAST_TIME,
        "end_min_music_slow_time": END_MIN_MUSIC_SLOW_TIME,
        "end_max_music_slow_time": END_MAX_MUSIC_SLOW_TIME,
    }


# ========================================================================
# resolve_music_windows
# ========================================================================


def test_default_flag_reproduces_constants():
    """The default object resolves to exactly the module-constant windows."""
    windows = resolve_music_windows(_default_flag_dict())
    assert windows == MusicWindows.defaults()
    assert windows.fast_min == MIN_MUSIC_FAST_TIME
    assert windows.fast_max == MAX_MUSIC_FAST_TIME
    assert windows.slow_min == MIN_MUSIC_SLOW_TIME
    assert windows.slow_max == MAX_MUSIC_SLOW_TIME
    assert windows.end_fast_min == END_MIN_MUSIC_FAST_TIME
    assert windows.end_fast_max == END_MAX_MUSIC_FAST_TIME
    assert windows.end_slow_min == END_MIN_MUSIC_SLOW_TIME
    assert windows.end_slow_max == END_MAX_MUSIC_SLOW_TIME


def test_non_dict_falls_back():
    for bad in (None, "x", 5, [], True):
        assert resolve_music_windows(bad) == MusicWindows.defaults()


def test_missing_key_falls_back_whole_set():
    partial = _default_flag_dict()
    del partial["min_music_fast_time"]
    assert resolve_music_windows(partial) == MusicWindows.defaults()


def test_malformed_value_falls_back_whole_set():
    for bad in (0, -1, "fast", None, True, float("nan"), float("inf")):
        flag = _default_flag_dict()
        flag["max_music_slow_time"] = bad
        assert resolve_music_windows(flag) == MusicWindows.defaults()


def test_min_gt_max_falls_back_whole_set():
    # invert each window pair in turn -> whole-set fallback
    for min_key, max_key in (
        ("min_music_fast_time", "max_music_fast_time"),
        ("min_music_slow_time", "max_music_slow_time"),
        ("end_min_music_fast_time", "end_max_music_fast_time"),
        ("end_min_music_slow_time", "end_max_music_slow_time"),
    ):
        flag = _default_flag_dict()
        flag[min_key], flag[max_key] = flag[max_key], flag[min_key]
        # only invert if they actually differ (all defaults do)
        assert resolve_music_windows(flag) == MusicWindows.defaults()


def test_min_eq_max_is_accepted():
    flag = _default_flag_dict()
    flag["min_music_fast_time"] = flag["max_music_fast_time"]
    windows = resolve_music_windows(flag)
    assert windows.fast_min == windows.fast_max == MAX_MUSIC_FAST_TIME


def test_valid_custom_windows_resolve():
    flag = {
        "min_music_fast_time": 3,
        "max_music_fast_time": 6,
        "min_music_slow_time": 13,
        "max_music_slow_time": 30,
        "end_min_music_fast_time": 4,
        "end_max_music_fast_time": 7,
        "end_min_music_slow_time": 10,
        "end_max_music_slow_time": 16,
    }
    windows = resolve_music_windows(flag)
    assert windows.slow_max == 30.0
    assert windows.fast_min == 3.0


# ========================================================================
# Characterization: music loop draws delays within the same ranges
# ========================================================================


def test_default_instance_reproduces_scheduling_ranges():
    """With default windows, _get_music_change_time stays within constant ranges."""
    game = _make_ffa()
    assert game.music_windows == MusicWindows.defaults()

    # Two players -> game_percent = 0 at start (early-game windows in effect).
    game.players = {"a": object(), "b": object()}
    game.dead_count = 0

    import time as _time

    # speed_up=True -> slow windows; speed_up=False -> fast windows.
    for speed_up, lo, hi in (
        (True, MIN_MUSIC_SLOW_TIME, MAX_MUSIC_SLOW_TIME),
        (False, MIN_MUSIC_FAST_TIME, MAX_MUSIC_FAST_TIME),
    ):
        game.speed_up = speed_up
        for _ in range(200):
            now = _time.time()
            delay = game._get_music_change_time() - now
            assert lo - 0.001 <= delay <= hi + 0.001


def test_end_game_windows_used_when_all_dead():
    """game_percent=1.0 -> end-game windows bound the delay."""
    game = _make_ffa()
    game.players = {"a": object(), "b": object(), "c": object()}
    game.dead_count = len(game.players)  # forces game_percent -> 1.0

    import time as _time

    for speed_up, lo, hi in (
        (True, END_MIN_MUSIC_SLOW_TIME, END_MAX_MUSIC_SLOW_TIME),
        (False, END_MIN_MUSIC_FAST_TIME, END_MAX_MUSIC_FAST_TIME),
    ):
        game.speed_up = speed_up
        for _ in range(200):
            now = _time.time()
            delay = game._get_music_change_time() - now
            assert lo - 0.001 <= delay <= hi + 0.001


# ========================================================================
# Init-frozen read + F6 swap seam
# ========================================================================


def test_windows_read_once_and_frozen():
    custom = {
        "min_music_fast_time": 2,
        "max_music_fast_time": 5,
        "min_music_slow_time": 8,
        "max_music_slow_time": 20,
        "end_min_music_fast_time": 3,
        "end_max_music_fast_time": 6,
        "end_min_music_slow_time": 6,
        "end_max_music_slow_time": 10,
    }
    with patch(
        "services.game_coordinator.games.base.read_object_flag",
        side_effect=lambda _domain, key, default, _game_id=None: custom if key == "windows" else default,
    ) as mock_read:
        game = _make_ffa()
    assert game.music_windows.fast_min == 2.0
    assert game.music_windows.slow_max == 20.0
    windows_calls = [c for c in mock_read.call_args_list if c.args[1] == "windows"]
    assert len(windows_calls) == 1


def test_malformed_flag_instance_falls_back():
    with patch(
        "services.game_coordinator.games.base.read_object_flag",
        side_effect=lambda _domain, key, default, _game_id=None: "garbage" if key == "windows" else default,
    ):
        game = _make_ffa()
    assert game.music_windows == MusicWindows.defaults()


def test_f6_atomic_swap_seam():
    """F6 swaps self.music_windows atomically; the loop picks up the new windows."""
    game = _make_ffa()
    game.players = {"a": object(), "b": object()}
    game.dead_count = 0
    game.speed_up = False  # fast windows

    import time as _time

    # Swap to the frantic-ish profile (longer fast windows).
    frantic = MusicWindows(
        fast_min=6,
        fast_max=11,
        slow_min=6,
        slow_max=14,
        end_fast_min=8,
        end_fast_max=14,
        end_slow_min=5,
        end_slow_max=8,
    )
    game.music_windows = frantic  # the atomic single-store swap F6 uses

    for _ in range(200):
        delay = game._get_music_change_time() - _time.time()
        assert 6 - 0.001 <= delay <= 11 + 0.001


# ========================================================================
# Preset variants in game.json are well-formed
# ========================================================================


def test_game_json_window_presets_well_formed():
    data = json.loads(GAME_JSON.read_text())
    flag = data["flags"]["windows"]
    variants = flag["variants"]

    # All three presets present, default is the default variant.
    assert set(variants) >= {"calm", "default", "frantic"}
    assert flag["defaultVariant"] == "default"

    # default variant must reproduce the module constants exactly.
    assert resolve_music_windows(variants["default"]) == MusicWindows.defaults()

    # every preset parses without falling back to defaults (i.e. is well-formed:
    # all positive, every min <= max).
    defaults = MusicWindows.defaults()
    for name, value in variants.items():
        resolved = resolve_music_windows(value)
        if name == "default":
            assert resolved == defaults
        else:
            # well-formed: did NOT silently revert to defaults
            assert resolved != defaults, f"preset {name} reverted to defaults (malformed)"
        # explicit pair ordering sanity
        assert resolved.fast_min <= resolved.fast_max
        assert resolved.slow_min <= resolved.slow_max
        assert resolved.end_fast_min <= resolved.end_fast_max
        assert resolved.end_slow_min <= resolved.end_slow_max


def test_preset_pacing_directions():
    """calm = slower pacing (longer slow / shorter fast); frantic = the reverse."""
    data = json.loads(GAME_JSON.read_text())
    variants = data["flags"]["windows"]["variants"]
    calm = resolve_music_windows(variants["calm"])
    default = resolve_music_windows(variants["default"])
    frantic = resolve_music_windows(variants["frantic"])

    # calm dwells longer in slow, shorter in fast than default.
    assert calm.slow_max >= default.slow_max
    assert calm.fast_max <= default.fast_max
    # frantic dwells shorter in slow, longer in fast than default.
    assert frantic.slow_max <= default.slow_max
    assert frantic.fast_max >= default.fast_max
