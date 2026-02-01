"""
Tests for Player Profile Manager (Issue #23)

Tests CRUD operations, Redis integration, and metrics.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from lib.player_profile import PlayerProfile, RoundResult
from lib.player_profile_manager import (
    MAX_HISTORY_ROUNDS,
    PlayerProfileManager,
    get_profile_manager,
)


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    redis = MagicMock()
    redis.get_json.return_value = None  # Default: no data
    redis.set_json.return_value = True  # Default: success
    redis.exists.return_value = False
    redis.delete.return_value = True
    return redis


@pytest.fixture
def profile_manager(mock_redis):
    """Create a PlayerProfileManager with mock Redis."""
    return PlayerProfileManager(redis_client=mock_redis)


class TestProfileKeyGeneration:
    """Test Redis key generation."""

    def test_profile_key(self, profile_manager):
        """Test profile key generation."""
        key = profile_manager._profile_key("AA:BB:CC:DD:EE:FF")
        assert key == "player:AA:BB:CC:DD:EE:FF:profile"

    def test_history_key(self, profile_manager):
        """Test history key generation."""
        key = profile_manager._history_key("AA:BB:CC:DD:EE:FF")
        assert key == "player:AA:BB:CC:DD:EE:FF:history"

    def test_session_key(self, profile_manager):
        """Test session key generation."""
        key = profile_manager._session_key("session_123")
        assert key == "session:session_123:players"


class TestLoadProfile:
    """Test profile loading."""

    def test_load_new_profile(self, profile_manager, mock_redis):
        """Test loading a profile that doesn't exist (creates new)."""
        mock_redis.get_json.return_value = None

        profile = profile_manager.load_profile("AA:BB:CC:DD:EE:FF")

        assert profile.serial == "AA:BB:CC:DD:EE:FF"
        assert profile.total_games == 0
        assert profile.performance_score == 50.0
        mock_redis.get_json.assert_called_once()

    def test_load_existing_profile(self, profile_manager, mock_redis):
        """Test loading an existing profile from Redis."""
        # Mock existing profile data
        existing_data = {
            "serial": "AA:BB:CC:DD:EE:FF",
            "first_seen": time.time(),
            "last_seen": time.time(),
            "ffa_total_games": 10,
            "ffa_wins": 7,
            "ffa_warnings": 15,
            "ffa_total_survival_time": 1200.0,
            "nonstop_total_games": 0,
            "nonstop_kills": 0,
            "nonstop_deaths": 0,
            "nonstop_best_streak": 0,
            "team_total_games": 0,
            "team_wins": 0,
            "total_warnings": 15,
            "average_battery_level": 100.0,
            "connection_stability_score": 100.0,
            "performance_score": 65.0,
            "reward_tier": "GOOD",
        }
        mock_redis.get_json.return_value = existing_data

        profile = profile_manager.load_profile("AA:BB:CC:DD:EE:FF")

        assert profile.serial == "AA:BB:CC:DD:EE:FF"
        assert profile.ffa_total_games == 10
        assert profile.ffa_wins == 7
        assert profile.performance_score == 65.0

    def test_load_profile_error_returns_new(self, profile_manager, mock_redis):
        """Test that load_profile returns new profile on error."""
        mock_redis.get_json.side_effect = Exception("Redis error")

        profile = profile_manager.load_profile("AA:BB:CC:DD:EE:FF")

        # Should return new profile as fallback
        assert profile.serial == "AA:BB:CC:DD:EE:FF"
        assert profile.total_games == 0


class TestSaveProfile:
    """Test profile saving."""

    def test_save_profile_success(self, profile_manager, mock_redis):
        """Test successfully saving a profile."""
        profile = PlayerProfile.create_new("AA:BB:CC:DD:EE:FF")
        mock_redis.set_json.return_value = True

        result = profile_manager.save_profile(profile)

        assert result is True
        mock_redis.set_json.assert_called_once()
        call_args = mock_redis.set_json.call_args
        assert "player:AA:BB:CC:DD:EE:FF:profile" in call_args[0][0]

    def test_save_profile_failure(self, profile_manager, mock_redis):
        """Test handling save failure."""
        profile = PlayerProfile.create_new("AA:BB:CC:DD:EE:FF")
        mock_redis.set_json.return_value = False

        result = profile_manager.save_profile(profile)

        assert result is False

    def test_save_profile_error(self, profile_manager, mock_redis):
        """Test handling save error."""
        profile = PlayerProfile.create_new("AA:BB:CC:DD:EE:FF")
        mock_redis.set_json.side_effect = Exception("Redis error")

        result = profile_manager.save_profile(profile)

        assert result is False


class TestUpdateStats:
    """Test stat update methods."""

    @patch.object(PlayerProfileManager, "load_profile")
    @patch.object(PlayerProfileManager, "save_profile")
    def test_update_ffa_stats(self, mock_save, mock_load, profile_manager):
        """Test updating FFA stats."""
        profile = PlayerProfile.create_new("test")
        mock_load.return_value = profile
        mock_save.return_value = True

        result = profile_manager.update_ffa_stats(
            serial="test",
            won=True,
            warnings=3,
            survival_time=120.5,
        )

        assert result is True
        assert profile.ffa_total_games == 1
        assert profile.ffa_wins == 1
        assert profile.ffa_warnings == 3
        assert profile.ffa_total_survival_time == 120.5
        mock_load.assert_called_once_with("test")
        mock_save.assert_called_once_with(profile)

    @patch.object(PlayerProfileManager, "load_profile")
    @patch.object(PlayerProfileManager, "save_profile")
    def test_update_nonstop_stats(self, mock_save, mock_load, profile_manager):
        """Test updating Nonstop stats."""
        profile = PlayerProfile.create_new("test")
        mock_load.return_value = profile
        mock_save.return_value = True

        result = profile_manager.update_nonstop_stats(
            serial="test",
            kills=15,
            deaths=8,
            warnings=5,
            best_streak=7,
        )

        assert result is True
        assert profile.nonstop_total_games == 1
        assert profile.nonstop_kills == 15
        assert profile.nonstop_deaths == 8
        assert profile.nonstop_best_streak == 7
        mock_load.assert_called_once_with("test")
        mock_save.assert_called_once_with(profile)

    @patch.object(PlayerProfileManager, "load_profile")
    @patch.object(PlayerProfileManager, "save_profile")
    def test_update_team_stats(self, mock_save, mock_load, profile_manager):
        """Test updating team stats."""
        profile = PlayerProfile.create_new("test")
        mock_load.return_value = profile
        mock_save.return_value = True

        result = profile_manager.update_team_stats(
            serial="test",
            won=True,
            warnings=2,
        )

        assert result is True
        assert profile.team_total_games == 1
        assert profile.team_wins == 1
        assert profile.total_warnings == 2
        mock_load.assert_called_once_with("test")
        mock_save.assert_called_once_with(profile)


class TestRoundHistory:
    """Test round history management."""

    def test_add_round_to_history(self, profile_manager, mock_redis):
        """Test adding a round result to history."""
        mock_redis.lpush_json.return_value = 1

        round_result = RoundResult(
            timestamp=time.time(),
            game_mode="FFA",
            player_count=4,
            won=True,
            alive=True,
            survival_time=120.0,
            warnings=2,
        )

        result = profile_manager.add_round_to_history("test", round_result)

        assert result is True
        mock_redis.lpush_json.assert_called_once()
        mock_redis.ltrim.assert_called_once_with(
            "player:test:history", 0, MAX_HISTORY_ROUNDS - 1
        )

    def test_get_round_history(self, profile_manager, mock_redis):
        """Test retrieving round history."""
        # Mock history data
        history_data = [
            {
                "timestamp": time.time(),
                "game_mode": "FFA",
                "player_count": 4,
                "won": True,
                "alive": True,
                "survival_time": 120.0,
                "warnings": 2,
                "kills": 0,
                "deaths": 0,
            },
            {
                "timestamp": time.time() - 100,
                "game_mode": "Teams",
                "player_count": 6,
                "won": False,
                "alive": False,
                "survival_time": 60.0,
                "warnings": 5,
                "kills": 0,
                "deaths": 0,
            },
        ]
        mock_redis.lrange_json.return_value = history_data

        history = profile_manager.get_round_history("test", limit=10)

        assert len(history) == 2
        assert isinstance(history[0], RoundResult)
        assert history[0].game_mode == "FFA"
        assert history[1].game_mode == "Teams"


class TestSessionManagement:
    """Test session management."""

    def test_add_to_session(self, profile_manager, mock_redis):
        """Test adding player to session."""
        result = profile_manager.add_to_session("session_1", "player_1")

        assert result is True
        mock_redis.sadd.assert_called_once_with("session:session_1:players", "player_1")

    def test_remove_from_session(self, profile_manager, mock_redis):
        """Test removing player from session."""
        result = profile_manager.remove_from_session("session_1", "player_1")

        assert result is True
        mock_redis.srem.assert_called_once_with("session:session_1:players", "player_1")

    def test_get_session_players(self, profile_manager, mock_redis):
        """Test getting all players in a session."""
        mock_redis.smembers.return_value = {"player_1", "player_2", "player_3"}

        players = profile_manager.get_session_players("session_1")

        assert len(players) == 3
        assert "player_1" in players
        assert "player_2" in players
        mock_redis.smembers.assert_called_once_with("session:session_1:players")


class TestDeleteProfile:
    """Test profile deletion."""

    def test_delete_profile_success(self, profile_manager, mock_redis):
        """Test successfully deleting a profile."""
        mock_redis.delete.return_value = True

        result = profile_manager.delete_profile("test")

        assert result is True
        # Should delete both profile and history
        assert mock_redis.delete.call_count == 2

    def test_delete_profile_not_found(self, profile_manager, mock_redis):
        """Test deleting a profile that doesn't exist."""
        mock_redis.delete.return_value = False

        result = profile_manager.delete_profile("test")

        assert result is False


class TestSingletonManager:
    """Test global profile manager singleton."""

    def test_get_profile_manager_creates_singleton(self, mock_redis):
        """Test that get_profile_manager creates and returns singleton."""
        # Reset singleton
        import lib.player_profile_manager as ppm

        ppm._profile_manager = None

        # Use mock Redis to avoid connection errors
        manager1 = get_profile_manager(redis_client=mock_redis)
        manager2 = get_profile_manager(redis_client=mock_redis)

        assert manager1 is manager2  # Same instance

    def test_get_profile_manager_with_custom_redis(self, mock_redis):
        """Test get_profile_manager with custom Redis client."""
        # Reset singleton
        import lib.player_profile_manager as ppm

        ppm._profile_manager = None

        manager = get_profile_manager(redis_client=mock_redis)

        assert manager.redis is mock_redis
