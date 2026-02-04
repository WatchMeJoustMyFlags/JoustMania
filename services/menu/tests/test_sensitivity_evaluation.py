"""Tests for sensitivity feature flag evaluation in menu servicer."""

from unittest.mock import MagicMock, patch

import pytest

from lib.types import Games


class TestEvaluateSensitivity:
    """Tests for sensitivity evaluation logic."""

    @pytest.fixture
    def mock_servicer(self):
        """Create a minimal mock servicer with _evaluate_sensitivity method."""
        # Import the method by creating a mock that has it
        from services.menu.servicer import MenuServicer

        # Create instance method reference for testing
        # We'll call it with a mock self
        return MenuServicer._evaluate_sensitivity

    def test_returns_admin_setting_when_flag_client_unavailable(self, mock_servicer):
        """When flag client is unavailable, should return admin setting."""
        mock_self = MagicMock()

        with patch("services.menu.servicer._get_flag_client", return_value=None):
            result = mock_servicer(mock_self, Games.JoustFFA, 4, 2)
            assert result == 2

    def test_returns_admin_setting_when_no_targeting_match(self, mock_servicer):
        """When no targeting rule matches (defaultVariant: null), should return admin setting."""
        mock_self = MagicMock()
        mock_client = MagicMock()
        # When defaultVariant is null and no rule matches, flagd returns the code default
        mock_client.get_integer_value.return_value = 3  # admin_setting passed as default

        with patch("services.menu.servicer._get_flag_client", return_value=mock_client):
            result = mock_servicer(mock_self, Games.JoustFFA, 4, 3)

            # Should return what the flag client returned (which is the admin setting as default)
            assert result == 3
            mock_client.get_integer_value.assert_called_once()
            # Verify context was passed
            call_args = mock_client.get_integer_value.call_args
            assert call_args[0][0] == "sensitivity"  # flag key
            assert call_args[0][1] == 3  # default value (admin setting)

    def test_returns_flag_value_when_targeting_matches(self, mock_servicer):
        """When a targeting rule matches, should return the flag value."""
        mock_self = MagicMock()
        mock_client = MagicMock()
        # Werewolf targeting rule returns 3 (fast)
        mock_client.get_integer_value.return_value = 3

        with patch("services.menu.servicer._get_flag_client", return_value=mock_client):
            result = mock_servicer(mock_self, Games.Werewolf, 6, 2)

            assert result == 3
            # Verify context includes game_mode
            call_args = mock_client.get_integer_value.call_args
            context = call_args[0][2]
            assert context.attributes["game_mode"] == "Werewolf"
            assert context.attributes["player_count"] == 6

    def test_clamps_value_to_valid_range(self, mock_servicer):
        """Should clamp sensitivity to 0-4 range."""
        mock_self = MagicMock()
        mock_client = MagicMock()

        with patch("services.menu.servicer._get_flag_client", return_value=mock_client):
            # Test clamping high value
            mock_client.get_integer_value.return_value = 10
            result = mock_servicer(mock_self, Games.JoustFFA, 4, 2)
            assert result == 4

            # Test clamping low value
            mock_client.get_integer_value.return_value = -5
            result = mock_servicer(mock_self, Games.JoustFFA, 4, 2)
            assert result == 0

    def test_returns_admin_setting_on_exception(self, mock_servicer):
        """When flag evaluation raises an exception, should return admin setting."""
        mock_self = MagicMock()
        mock_client = MagicMock()
        mock_client.get_integer_value.side_effect = Exception("flagd unreachable")

        with patch("services.menu.servicer._get_flag_client", return_value=mock_client):
            result = mock_servicer(mock_self, Games.JoustFFA, 4, 2)
            assert result == 2

    def test_passes_correct_context_attributes(self, mock_servicer):
        """Should pass game_mode and player_count in evaluation context."""
        mock_self = MagicMock()
        mock_client = MagicMock()
        mock_client.get_integer_value.return_value = 2

        with patch("services.menu.servicer._get_flag_client", return_value=mock_client):
            mock_servicer(mock_self, Games.Zombies, 8, 1)

            call_args = mock_client.get_integer_value.call_args
            context = call_args[0][2]
            assert context.attributes["game_mode"] == "Zombies"
            assert context.attributes["player_count"] == 8
