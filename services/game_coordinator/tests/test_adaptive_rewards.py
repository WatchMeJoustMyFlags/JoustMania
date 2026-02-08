"""
Tests for adaptive reward system.
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
        event_publisher=MagicMock(),
        audio_client=MagicMock(),
        sensitivity=2,  # MEDIUM
        initial_players=[],
    )
    game.start_time = 0.0
    game.players = {}
    return game


@patch("lib.feature_flags.api")
def test_adaptive_threshold_disabled(mock_api, mock_game_instance, mock_player):
    """Test that adaptive threshold is not applied when disabled."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = False

    mock_game_instance.players[mock_player.serial] = mock_player

    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    assert adjusted == base_threshold
    mock_client.get_boolean_value.assert_called_once_with("enable_adaptive_rewards", False)


@patch("lib.feature_flags.api")
def test_adaptive_threshold_enabled_neutral(mock_api, mock_game_instance, mock_player):
    """Test that neutral adjustment (0.0) doesn't change threshold."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = 0.0

    mock_game_instance.players[mock_player.serial] = mock_player

    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    assert adjusted == base_threshold


@patch("lib.feature_flags.api")
def test_adaptive_threshold_enabled_buff_weak_player(mock_api, mock_game_instance, mock_player):
    """Test that positive adjustment makes it easier (higher threshold)."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = 0.3

    mock_game_instance.players[mock_player.serial] = mock_player

    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    assert adjusted == 2.3
    assert adjusted > base_threshold


@patch("lib.feature_flags.api")
def test_adaptive_threshold_enabled_nerf_strong_player(mock_api, mock_game_instance, mock_player):
    """Test that negative adjustment makes it harder (lower threshold)."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = -0.3

    mock_game_instance.players[mock_player.serial] = mock_player

    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    assert adjusted == 1.7
    assert adjusted < base_threshold


@patch("lib.feature_flags.api")
def test_adaptive_threshold_clamping(mock_api, mock_game_instance, mock_player):
    """Test that adjusted threshold is clamped to sane range."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = -10.0

    mock_game_instance.players[mock_player.serial] = mock_player

    base_threshold = 1.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    assert adjusted == 0.5


@patch("lib.feature_flags.api")
def test_adaptive_threshold_fallback_on_error(mock_api, mock_game_instance, mock_player):
    """Test that base threshold is returned on flag evaluation error."""
    mock_api.get_client.side_effect = Exception("flagd connection error")

    mock_game_instance.players[mock_player.serial] = mock_player

    base_threshold = 2.0
    adjusted = mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, base_threshold, 10.0)

    assert adjusted == base_threshold


@patch("lib.feature_flags.api")
def test_adaptive_threshold_player_context_values(mock_api, mock_game_instance, mock_player):
    """Test that player context is built correctly for flag evaluation."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = True
    mock_client.get_float_value.return_value = 0.0

    mock_game_instance.players[mock_player.serial] = mock_player
    mock_game_instance.start_time = 0.0

    current_time = 60.0
    mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 2.0, current_time)

    # Verify get_float_value was called with a context
    assert mock_client.get_float_value.called
    call_args = mock_client.get_float_value.call_args
    context = call_args[0][2]  # Third argument is context

    # Verify context has expected player attributes (not game_mode/controller_count)
    assert context.targeting_key == mock_player.serial
    assert context.attributes["serial"] == mock_player.serial
    assert context.attributes["player_name"] == mock_player.serial
    assert "win_rate" in context.attributes
    assert "warnings_per_minute" in context.attributes
    # game_mode and controller_count come from transaction context, not per-evaluation
    assert "game_mode" not in context.attributes
    assert "controller_count" not in context.attributes


@patch("lib.feature_flags.api")
def test_adaptive_threshold_uses_domain_scoped_client(mock_api, mock_game_instance, mock_player):
    """Test that the domain-scoped client is used (game_settings domain)."""
    mock_client = MagicMock()
    mock_api.get_client.return_value = mock_client
    mock_client.get_boolean_value.return_value = False

    mock_game_instance.players[mock_player.serial] = mock_player

    mock_game_instance._apply_adaptive_threshold_adjustment(mock_player, 2.0, 10.0)

    # Should call get_client with domain="game_settings"
    mock_api.get_client.assert_called_with(domain="game_settings")
