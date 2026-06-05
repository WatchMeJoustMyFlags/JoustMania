"""Unit tests for effect timing flags (F7, #766).

Characterization: the flagd defaults reproduce the original hardcoded effect
timings. Fallback: malformed flag values revert to the hardcoded defaults.
"""

from unittest.mock import MagicMock

import services.controller_manager.feedback_manager as fm
from services.controller_manager.feedback_manager import (
    DEFAULT_EFFECT_TIMINGS,
    _read_effect_timing,
    get_effect_timing_ms,
    init_effect_timings,
)


def test_defaults_match_original_constants():
    """The promoted defaults must equal the original inline magic numbers."""
    assert DEFAULT_EFFECT_TIMINGS == {
        "warning_flash_ms": 200,
        "warning_vibration_ms": 200,
        "death_rumble_ms": 150,
        "death_red_hold_ms": 300,
        "death_fade_ms": 700,
    }


def test_get_effect_timing_falls_back_to_default_for_unknown_key():
    assert get_effect_timing_ms("does_not_exist") == 0


def test_read_effect_timing_accepts_valid_value():
    client = MagicMock()
    client.get_integer_value.return_value = 250
    assert _read_effect_timing(client, "effect.death_rumble_ms", 150) == 250


def test_read_effect_timing_rejects_non_positive():
    client = MagicMock()
    client.get_integer_value.return_value = 0
    assert _read_effect_timing(client, "effect.death_rumble_ms", 150) == 150

    client.get_integer_value.return_value = -5
    assert _read_effect_timing(client, "effect.death_rumble_ms", 150) == 150


def test_read_effect_timing_rejects_non_int():
    client = MagicMock()
    client.get_integer_value.return_value = "fast"
    assert _read_effect_timing(client, "effect.death_fade_ms", 700) == 700


def test_read_effect_timing_falls_back_on_read_error():
    client = MagicMock()
    client.get_integer_value.side_effect = RuntimeError("flagd down")
    assert _read_effect_timing(client, "effect.warning_flash_ms", 200) == 200


def test_init_effect_timings_falls_back_when_flagd_unavailable(monkeypatch):
    """If the controller domain cannot be initialized, defaults are used."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("no flagd")

    monkeypatch.setattr("lib.feature_flags.init_flag_domain", _boom, raising=False)
    init_effect_timings()
    for key, default in DEFAULT_EFFECT_TIMINGS.items():
        assert get_effect_timing_ms(key) == default


def test_init_effect_timings_reads_overrides(monkeypatch):
    """Valid flag values override the cached timings."""
    overrides = {
        "effect.warning_flash_ms": 111,
        "effect.warning_vibration_ms": 222,
        "effect.death_rumble_ms": 99,
        "effect.death_red_hold_ms": 333,
        "effect.death_fade_ms": 555,
    }
    client = MagicMock()
    client.get_integer_value.side_effect = lambda name, default, _ctx: overrides.get(name, default)

    monkeypatch.setattr("lib.feature_flags.init_flag_domain", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr("lib.feature_flags.get_flag_client", lambda *_a, **_k: client, raising=False)

    init_effect_timings()
    assert get_effect_timing_ms("warning_flash_ms") == 111
    assert get_effect_timing_ms("death_fade_ms") == 555

    # Restore defaults for isolation from other tests.
    fm._effect_timings = dict(DEFAULT_EFFECT_TIMINGS)
