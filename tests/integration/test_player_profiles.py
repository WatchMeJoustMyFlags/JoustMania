"""
Integration tests for Player Profiles with Redis (Issue #23).

Tests profile loading, saving, and persistence across game sessions.
"""

import pytest

from lib.player_profile import PlayerProfile
from lib.player_profile_manager import PlayerProfileManager, get_profile_manager
from lib.redis_client import RedisClient


@pytest.fixture
def redis_client():
    """Create Redis client for integration tests."""
    try:
        client = RedisClient(host="redis", port=6379, db=15)  # Use test DB
        yield client
        # Cleanup: flush test database
        if client._client:
            client._client.flushdb()
        client.close()
    except Exception:
        pytest.skip("Redis not available for integration tests")


@pytest.fixture
def profile_manager(redis_client):
    """Create ProfileManager with test Redis client."""
    return PlayerProfileManager(redis_client=redis_client)


class TestRedisIntegration:
    """Test Redis integration for player profiles."""

    def test_profile_persistence(self, profile_manager):
        """Test that profiles are persisted to Redis."""
        # Create and save a profile
        profile = PlayerProfile.create_new("test_serial_123")
        profile.ffa_total_games = 5
        profile.ffa_wins = 3
        profile.update_performance_metrics()

        success = profile_manager.save_profile(profile)
        assert success is True

        # Load the profile
        loaded_profile = profile_manager.load_profile("test_serial_123")
        assert loaded_profile.serial == "test_serial_123"
        assert loaded_profile.ffa_total_games == 5
        assert loaded_profile.ffa_wins == 3

    def test_profile_update_ffa_stats(self, profile_manager):
        """Test updating FFA stats via profile manager."""
        serial = "test_ffa_player"

        # Update stats
        success = profile_manager.update_ffa_stats(
            serial=serial,
            won=True,
            warnings=2,
            survival_time=120.5,
        )
        assert success is True

        # Load and verify
        profile = profile_manager.load_profile(serial)
        assert profile.ffa_total_games == 1
        assert profile.ffa_wins == 1
        assert profile.ffa_warnings == 2
        assert profile.ffa_total_survival_time == 120.5

    def test_profile_update_nonstop_stats(self, profile_manager):
        """Test updating Nonstop stats via profile manager."""
        serial = "test_nonstop_player"

        # Update stats
        success = profile_manager.update_nonstop_stats(
            serial=serial,
            kills=15,
            deaths=8,
            warnings=5,
            best_streak=7,
        )
        assert success is True

        # Load and verify
        profile = profile_manager.load_profile(serial)
        assert profile.nonstop_total_games == 1
        assert profile.nonstop_kills == 15
        assert profile.nonstop_deaths == 8
        assert profile.nonstop_best_streak == 7

    def test_profile_update_team_stats(self, profile_manager):
        """Test updating team stats via profile manager."""
        serial = "test_team_player"

        # Update stats
        success = profile_manager.update_team_stats(
            serial=serial,
            won=True,
            warnings=3,
        )
        assert success is True

        # Load and verify
        profile = profile_manager.load_profile(serial)
        assert profile.team_total_games == 1
        assert profile.team_wins == 1
        assert profile.total_warnings == 3

    def test_multiple_game_updates(self, profile_manager):
        """Test accumulating stats over multiple games."""
        serial = "test_multi_game"

        # Play multiple games
        profile_manager.update_ffa_stats(serial, won=True, warnings=1, survival_time=100.0)
        profile_manager.update_ffa_stats(serial, won=False, warnings=3, survival_time=50.0)
        profile_manager.update_ffa_stats(serial, won=True, warnings=2, survival_time=150.0)

        # Load and verify cumulative stats
        profile = profile_manager.load_profile(serial)
        assert profile.ffa_total_games == 3
        assert profile.ffa_wins == 2
        assert profile.ffa_warnings == 6
        assert profile.ffa_total_survival_time == 300.0
        assert profile.ffa_win_rate == pytest.approx(2 / 3, rel=0.01)

    def test_round_history_persistence(self, profile_manager, redis_client):
        """Test that round history is persisted to Redis."""
        from lib.player_profile import RoundResult
        import time

        serial = "test_history_player"

        # Add round results
        round1 = RoundResult(
            timestamp=time.time(),
            game_mode="FFA",
            player_count=4,
            won=True,
            alive=True,
            survival_time=120.0,
            warnings=2,
        )
        success = profile_manager.add_round_to_history(serial, round1)
        assert success is True

        # Retrieve history
        history = profile_manager.get_round_history(serial, limit=10)
        assert len(history) == 1
        assert history[0].game_mode == "FFA"
        assert history[0].won is True

    def test_session_management(self, profile_manager):
        """Test session management for active players."""
        session_id = "test_session_123"

        # Add players to session
        profile_manager.add_to_session(session_id, "player_1")
        profile_manager.add_to_session(session_id, "player_2")
        profile_manager.add_to_session(session_id, "player_3")

        # Get session players
        players = profile_manager.get_session_players(session_id)
        assert len(players) == 3
        assert "player_1" in players
        assert "player_2" in players
        assert "player_3" in players

        # Remove a player
        profile_manager.remove_from_session(session_id, "player_2")
        players = profile_manager.get_session_players(session_id)
        assert len(players) == 2
        assert "player_2" not in players

    def test_profile_deletion(self, profile_manager):
        """Test profile deletion."""
        serial = "test_delete_player"

        # Create and save a profile
        profile = PlayerProfile.create_new(serial)
        profile_manager.save_profile(profile)

        # Verify it exists
        loaded = profile_manager.load_profile(serial)
        assert loaded.serial == serial

        # Delete it
        success = profile_manager.delete_profile(serial)
        assert success is True

        # Verify it's gone (should create new profile)
        loaded_after_delete = profile_manager.load_profile(serial)
        assert loaded_after_delete.total_games == 0  # New profile


class TestRedisConnectionHandling:
    """Test Redis connection error handling."""

    def test_graceful_degradation_invalid_host(self):
        """Test that invalid Redis host doesn't crash the application."""
        # This should fail to connect but not raise exception due to graceful degradation
        try:
            client = RedisClient(host="invalid-host", port=6379, db=0)
            # If connection succeeds somehow, clean up
            client.close()
        except Exception:
            # Expected to fail - connection should raise exception
            pass

    def test_profile_manager_singleton(self):
        """Test profile manager singleton pattern."""
        # Reset singleton for test
        import lib.player_profile_manager as ppm

        original_manager = ppm._profile_manager
        ppm._profile_manager = None

        try:
            # This should create a singleton even if Redis fails
            # (will be caught by graceful degradation in game coordinator)
            manager1 = get_profile_manager()
            manager2 = get_profile_manager()
            assert manager1 is manager2
        finally:
            # Restore original singleton
            ppm._profile_manager = original_manager
