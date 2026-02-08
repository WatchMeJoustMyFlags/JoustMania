"""
Tests for adaptive reward system.

Tests the cached boolean listener, background adjustment computation,
and simplified hot-path threshold adjustment.
"""

import asyncio
import contextlib
from unittest.mock import MagicMock, patch

import pytest

from services.game_coordinator.games import base as base_module
from services.game_coordinator.games.base import Player


@pytest.fixture
def mock_player():
    """Create a mock player for testing."""
    return Player(
        serial="AA:BB:CC:DD:EE:FF",
        team=0,
        alive=True,
        color=(255, 0, 0),
        warning_count=3,
        analytics=None,
    )


@pytest.fixture
def mock_game_instance():
    """Create a minimal mock game instance for testing."""
    from services.game_coordinator.games.ffa import FFAGame

    game = FFAGame(
        controller_manager_client=MagicMock(),
        event_publisher=MagicMock(),
        audio_client=MagicMock(),
        sensitivity=2,  # MEDIUM
        initial_players=[],
    )
    game.start_time = 0.0
    game.players = {}
    return game


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state between tests."""
    original_enabled = base_module._adaptive_rewards_enabled
    original_initialized = base_module._adaptive_rewards_listener_initialized
    yield
    base_module._adaptive_rewards_enabled = original_enabled
    base_module._adaptive_rewards_listener_initialized = original_initialized


# ========================================================================
# Hot-path tests (_apply_adaptive_threshold_adjustment)
# ========================================================================


def test_hot_path_disabled(mock_game_instance, mock_player):
    """When adaptive rewards disabled, return base threshold unchanged."""
    base_module._adaptive_rewards_enabled = False
    mock_game_instance.players[mock_player.serial] = mock_player

    result = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 2.0, 10.0)

    assert result == 2.0


def test_hot_path_enabled_no_cached_adjustment(mock_game_instance, mock_player):
    """When enabled but no cached adjustment, return base threshold."""
    base_module._adaptive_rewards_enabled = True
    mock_game_instance.players[mock_player.serial] = mock_player
    # No entry in _player_adjustments

    result = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 2.0, 10.0)

    assert result == 2.0


def test_hot_path_positive_adjustment(mock_game_instance, mock_player):
    """Positive cached adjustment makes threshold higher (easier)."""
    base_module._adaptive_rewards_enabled = True
    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance._player_adjustments[mock_player.serial] = 0.3

    result = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 2.0, 10.0)

    assert result == 2.3


def test_hot_path_negative_adjustment(mock_game_instance, mock_player):
    """Negative cached adjustment makes threshold lower (harder)."""
    base_module._adaptive_rewards_enabled = True
    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance._player_adjustments[mock_player.serial] = -0.3

    result = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 2.0, 10.0)

    assert result == 1.7


def test_hot_path_clamping_low(mock_game_instance, mock_player):
    """Adjusted threshold is clamped to minimum 0.5."""
    base_module._adaptive_rewards_enabled = True
    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance._player_adjustments[mock_player.serial] = -10.0

    result = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 1.0, 10.0)

    assert result == 0.5


def test_hot_path_clamping_high(mock_game_instance, mock_player):
    """Adjusted threshold is clamped to maximum 5.0."""
    base_module._adaptive_rewards_enabled = True
    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance._player_adjustments[mock_player.serial] = 10.0

    result = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 2.0, 10.0)

    assert result == 5.0


# ========================================================================
# Background computation tests (_compute_player_adjustments)
# ========================================================================


@patch("lib.feature_flags.api")
def test_compute_adjustments_populates_cache(mock_api, mock_game_instance, mock_player):
    """Background computation populates _player_adjustments cache."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_float_value.return_value = 0.3

    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance.start_time = 0.0

    mock_game_instance._compute_player_adjustments()

    assert mock_game_instance._player_adjustments[mock_player.serial] == 0.3
    mock_client.get_float_value.assert_called_once()


@patch("lib.feature_flags.api")
def test_compute_adjustments_skips_dead_players(mock_api, mock_game_instance, mock_player):
    """Background computation skips dead players."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client

    mock_player.alive = False
    mock_game_instance.players[mock_player.serial] = mock_player

    mock_game_instance._compute_player_adjustments()

    mock_client.get_float_value.assert_not_called()


@patch("lib.feature_flags.api")
def test_compute_adjustments_builds_player_context(mock_api, mock_game_instance, mock_player):
    """Background computation builds player context with correct attributes."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_float_value.return_value = 0.0

    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance.start_time = 0.0

    mock_game_instance._compute_player_adjustments()

    call_args = mock_client.get_float_value.call_args
    context = call_args[0][2]
    assert context.targeting_key == mock_player.serial
    assert context.attributes["serial"] == mock_player.serial
    assert context.attributes["player_name"] == mock_player.serial
    assert "win_rate" in context.attributes
    assert "warnings_per_minute" in context.attributes


@patch("lib.feature_flags.api")
def test_compute_adjustments_error_fallback(mock_api, mock_game_instance, mock_player):
    """Background computation handles errors gracefully."""
    mock_api.get_client.side_effect = Exception("flagd down")

    mock_game_instance.players[mock_player.serial] = mock_player

    # Should not raise
    mock_game_instance._compute_player_adjustments()

    # Cache should remain empty
    assert mock_player.serial not in mock_game_instance._player_adjustments


@patch("lib.feature_flags.api")
def test_compute_adjustments_uses_domain_scoped_client(mock_api, mock_game_instance, mock_player):
    """Background computation uses game_settings domain client."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_float_value.return_value = 0.0

    mock_game_instance.players[mock_player.serial] = mock_player

    mock_game_instance._compute_player_adjustments()

    mock_api.get_client.assert_called_with(domain="game_settings")


# ========================================================================
# Listener tests
# ========================================================================


@patch("lib.feature_flags.api")
def test_listener_updates_cached_boolean(mock_api):
    """Flag change handler updates module-level cached boolean."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True

    base_module._adaptive_rewards_enabled = False
    base_module._on_game_settings_flags_changed(None)

    assert base_module._adaptive_rewards_enabled is True


@patch("lib.feature_flags.api")
def test_listener_skips_unrelated_flag_changes(mock_api):
    """Flag change handler ignores changes to unrelated flags."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client

    base_module._adaptive_rewards_enabled = False

    event = MagicMock()
    event.flags_changed = ["some_other_flag"]
    base_module._on_game_settings_flags_changed(event)

    # Should not call get_boolean_value since our flag wasn't changed
    mock_client.get_boolean_value.assert_not_called()
    assert base_module._adaptive_rewards_enabled is False


@patch("lib.feature_flags.api")
def test_listener_evaluates_when_our_flag_changed(mock_api):
    """Flag change handler re-evaluates when enable_adaptive_rewards changes."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True

    base_module._adaptive_rewards_enabled = False

    event = MagicMock()
    event.flags_changed = ["enable_adaptive_rewards", "other_flag"]
    base_module._on_game_settings_flags_changed(event)

    mock_client.get_boolean_value.assert_called_once()
    assert base_module._adaptive_rewards_enabled is True


@patch("lib.feature_flags.api")
def test_listener_handles_error_gracefully(mock_api):
    """Flag change handler handles errors without crashing."""
    mock_api.get_client.side_effect = Exception("connection lost")

    base_module._adaptive_rewards_enabled = False
    base_module._on_game_settings_flags_changed(None)

    # Should remain False on error
    assert base_module._adaptive_rewards_enabled is False


# ========================================================================
# Background loop integration test
# ========================================================================


@pytest.mark.asyncio
@patch("lib.feature_flags.api")
async def test_adaptive_rewards_loop_computes_when_enabled(mock_api, mock_game_instance, mock_player):
    """Background loop calls _compute_player_adjustments when enabled."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_float_value.return_value = 0.2

    base_module._adaptive_rewards_enabled = True
    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance.running = True

    # Run loop for a short time then cancel
    task = asyncio.create_task(mock_game_instance._adaptive_rewards_loop())
    await asyncio.sleep(0.1)
    mock_game_instance.running = False
    await asyncio.sleep(0.1)

    if not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert mock_game_instance._player_adjustments.get(mock_player.serial) == 0.2
