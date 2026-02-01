"""
Tests for adaptive reward system (Phase 52).
"""

from unittest.mock import MagicMock, patch

import pytest

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
        settings_client=MagicMock(),
        event_publisher=MagicMock(),
        audio_client=MagicMock(),
        sensitivity=2,  # MEDIUM
        initial_players=[],
    )
    game.start_time = 0.0
    game.players = {}
    return game


@patch("lib.feature_flags.get_feature_flag_client")
def test_adaptive_threshold_disabled(mock_get_client, mock_game_instance, mock_player):
    """Test that adaptive threshold is not applied when disabled."""
    # Setup mock client to return adaptive_enabled=False
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = False

    mock_game_instance.players[mock_player.serial] = mock_player

    # Apply adjustment
    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    # Should return unchanged threshold
    assert adjusted == base_threshold
    mock_client.get_boolean_value.assert_called_once_with("enable_adaptive_rewards", False)


@patch("lib.feature_flags.get_feature_flag_client")
def test_adaptive_threshold_enabled_neutral(mock_get_client, mock_game_instance, mock_player):
    """Test that neutral adjustment (0.0) doesn't change threshold."""
    # Setup mock client
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = 0.0  # Neutral adjustment

    mock_game_instance.players[mock_player.serial] = mock_player

    # Apply adjustment
    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    # Should return unchanged threshold
    assert adjusted == base_threshold


@patch("lib.feature_flags.get_feature_flag_client")
def test_adaptive_threshold_enabled_buff_weak_player(mock_get_client, mock_game_instance, mock_player):
    """Test that positive adjustment makes it easier (higher threshold)."""
    # Setup mock client
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = 0.3  # Buff weak player

    mock_game_instance.players[mock_player.serial] = mock_player

    # Apply adjustment
    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    # Should increase threshold (easier to survive)
    assert adjusted == 2.3
    assert adjusted > base_threshold


@patch("lib.feature_flags.get_feature_flag_client")
def test_adaptive_threshold_enabled_nerf_strong_player(mock_get_client, mock_game_instance, mock_player):
    """Test that negative adjustment makes it harder (lower threshold)."""
    # Setup mock client
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = -0.3  # Nerf strong player

    mock_game_instance.players[mock_player.serial] = mock_player

    # Apply adjustment
    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    # Should decrease threshold (harder to survive)
    assert adjusted == 1.7
    assert adjusted < base_threshold


@patch("lib.feature_flags.get_feature_flag_client")
def test_adaptive_threshold_clamping(mock_get_client, mock_game_instance, mock_player):
    """Test that adjusted threshold is clamped to sane range."""
    # Setup mock client with extreme adjustment
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = -10.0  # Extreme nerf

    mock_game_instance.players[mock_player.serial] = mock_player

    # Apply adjustment with low base threshold
    base_threshold = 1.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    # Should be clamped to minimum (0.5)
    assert adjusted == 0.5


@patch("lib.feature_flags.get_feature_flag_client")
def test_adaptive_threshold_fallback_on_error(mock_get_client, mock_game_instance, mock_player):
    """Test that base threshold is returned on flag evaluation error."""
    # Setup mock client to throw exception
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_boolean_value.side_effect = Exception("flagd connection error")

    mock_game_instance.players[mock_player.serial] = mock_player

    # Apply adjustment
    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    # Should return unchanged threshold (fallback)
    assert adjusted == base_threshold


@patch("lib.feature_flags.get_feature_flag_client")
def test_adaptive_threshold_player_context_values(mock_get_client, mock_game_instance, mock_player):
    """Test that player context is built correctly for flag evaluation."""
    # Setup mock client
    mock_client = MagicMock()
    mock_get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = 0.0

    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance.start_time = 0.0

    # Apply adjustment
    current_time = 60.0  # 60 seconds into game
    mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 2.0, current_time)

    # Verify get_float_value was called with a context
    assert mock_client.get_float_value.called
    call_args = mock_client.get_float_value.call_args
    context = call_args[0][2]  # Third argument is context

    # Verify context has expected attributes
    assert context.targeting_key == mock_player.serial
    assert context.attributes["serial"] == mock_player.serial
    assert context.attributes["game_mode"] == "FFA"
    assert "win_rate" in context.attributes
    assert "warnings_per_minute" in context.attributes
