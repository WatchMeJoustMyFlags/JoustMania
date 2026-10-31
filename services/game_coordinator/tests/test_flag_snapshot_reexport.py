"""games/flag_snapshot.py is the one home of the flag parsers; base re-exports them (#2359)."""

import pytest

from services.game_coordinator.games import base, flag_snapshot

MOVED = [
    "resolve_base_thresholds",
    "resolve_mode_thresholds",
    "resolve_non_negative_duration",
    "resolve_music_windows",
    "resolve_ramp_seconds",
    "resolve_shield_activation",
    "resolve_charge_expiry",
    "resolve_hold_value",
    "resolve_shield_limit",
    "_read_volume_flag",
    "MusicWindows",
    "SHIELD_HOLD_FLAGS",
    "SHIELD_HOLD_CUE_FLAGS",
]


@pytest.mark.parametrize("name", MOVED)
def test_base_reexports_the_flag_snapshot_object(name):
    assert getattr(base, name) is getattr(flag_snapshot, name)


@pytest.mark.parametrize("name", ["read_float_flag", "read_int_flag", "read_object_flag", "read_string_flag"])
def test_readers_stay_patchable_on_base(name):
    """Tests and games/shields.py patch/call the readers as ``base.read_*_flag``."""
    assert callable(getattr(base, name))
    assert not hasattr(flag_snapshot, name)
