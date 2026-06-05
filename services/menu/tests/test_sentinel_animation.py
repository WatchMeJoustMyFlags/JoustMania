"""Unit tests for sentinel animation flags (F7, #766).

Characterization: SentinelAnimation defaults reproduce the original constants.
Fallback: malformed flag values revert to the hardcoded defaults.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.menu.idle_monitor import (
    _SENTINEL_BREATH_PERIOD,
    _SENTINEL_HUE_PERIOD,
    _SENTINEL_MAX_BRIGHTNESS,
    _SENTINEL_MIN_BRIGHTNESS,
    _SENTINEL_UPDATE_HZ,
    IdleMonitor,
    SentinelAnimation,
    read_sentinel_animation,
)


def test_defaults_match_original_constants():
    anim = SentinelAnimation()
    assert anim.update_hz == _SENTINEL_UPDATE_HZ == 10
    assert anim.min_brightness == _SENTINEL_MIN_BRIGHTNESS == 0.09
    assert anim.max_brightness == _SENTINEL_MAX_BRIGHTNESS == 0.30
    assert anim.breath_period_seconds == _SENTINEL_BREATH_PERIOD == 4.0
    assert anim.hue_period_seconds == _SENTINEL_HUE_PERIOD == 30.0


def test_read_with_none_client_returns_defaults():
    assert read_sentinel_animation(None) == SentinelAnimation()


def test_read_valid_overrides():
    values = {
        "sentinel.update_hz": 20,
        "sentinel.min_brightness": 0.1,
        "sentinel.max_brightness": 0.5,
        "sentinel.breath_period_seconds": 2.0,
        "sentinel.hue_period_seconds": 60.0,
    }
    client = MagicMock()
    client.get_float_value.side_effect = lambda name, default: values.get(name, default)

    anim = read_sentinel_animation(client)
    assert anim.update_hz == 20
    assert anim.min_brightness == 0.1
    assert anim.max_brightness == 0.5
    assert anim.breath_period_seconds == 2.0
    assert anim.hue_period_seconds == 60.0


def test_non_positive_period_falls_back():
    client = MagicMock()
    client.get_float_value.side_effect = lambda name, default: 0 if name == "sentinel.update_hz" else default
    anim = read_sentinel_animation(client)
    assert anim.update_hz == _SENTINEL_UPDATE_HZ


def test_brightness_out_of_range_falls_back():
    client = MagicMock()
    client.get_float_value.side_effect = lambda name, default: 1.5 if name == "sentinel.max_brightness" else default
    anim = read_sentinel_animation(client)
    # max reverted to default; min unchanged from default -> ordering preserved
    assert anim.max_brightness == _SENTINEL_MAX_BRIGHTNESS
    assert anim.min_brightness == _SENTINEL_MIN_BRIGHTNESS


def test_inverted_brightness_range_resets_both():
    client = MagicMock()
    overrides = {"sentinel.min_brightness": 0.4, "sentinel.max_brightness": 0.2}
    client.get_float_value.side_effect = lambda name, default: overrides.get(name, default)
    anim = read_sentinel_animation(client)
    assert anim.min_brightness == _SENTINEL_MIN_BRIGHTNESS
    assert anim.max_brightness == _SENTINEL_MAX_BRIGHTNESS


def test_read_error_falls_back():
    client = MagicMock()
    client.get_float_value.side_effect = RuntimeError("flagd down")
    anim = read_sentinel_animation(client)
    assert anim == SentinelAnimation()


@pytest.mark.asyncio
async def test_sentinel_loop_reads_animation_once():
    """The pulse loop snapshots the animation getter once at start (init-frozen)."""
    state_manager = MagicMock()
    state_manager.led.set_color = AsyncMock(return_value=True)

    calls = {"n": 0}

    def getter():
        calls["n"] += 1
        # Fast update rate so the loop iterates quickly under test.
        return SentinelAnimation(update_hz=1000)

    monitor = IdleMonitor(
        state_manager=state_manager,
        get_idle_enabled=lambda: True,
        get_idle_timeout=lambda: 15,
        get_sentinel_count=lambda: 1,
        get_rotation_minutes=lambda: 5,
        get_sentinel_animation=getter,
    )
    monitor._sentinel_serials = ["s1"]

    task = asyncio.create_task(monitor._sentinel_pulse_loop())
    await asyncio.sleep(0.02)
    task.cancel()
    # The loop swallows CancelledError to clean up LEDs (NOSONAR pattern), so
    # awaiting completes normally rather than re-raising.
    await task

    # Getter consulted exactly once despite many loop iterations.
    assert calls["n"] == 1
    assert state_manager.led.set_color.await_count >= 1
