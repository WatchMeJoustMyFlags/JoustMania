"""
Unit tests for player_context.py

Tests context building for feature flag evaluation with player profiles.
"""

import pytest
from unittest.mock import Mock

from lib.player_context import build_player_context, build_game_context
from lib.player_profile import PlayerProfile


class MockPlayer:
    """Mock Player object for testing."""

    def __init__(
        self,
        serial: str = "AA:BB:CC:DD:EE:FF",
        alive: bool = True,
        grace_until: float = 0.0,
        warning_count: int = 0,
        profile: PlayerProfile | None = None,
    ):
        self.serial = serial
        self.alive = alive
        self.grace_until = grace_until
        self.warning_count = warning_count
        self.profile = profile


class TestBuildPlayerContext:
    """Tests for build_player_context()"""

    def test_basic_context_without_profile(self):
        """Test context building for player without profile (defaults)."""
        player = MockPlayer(serial="AA:BB:CC:DD:EE:FF", alive=True, warning_count=0)

        context = build_player_context(
            player=player,
            game_mode="FFA",
            controller_count=4,
            game_duration_seconds=60.0,
        )

        # Check targeting key
        assert context.targeting_key == "AA:BB:CC:DD:EE:FF"

        # Check attributes
        attrs = context.attributes
        assert attrs["serial"] == "AA:BB:CC:DD:EE:FF"
        assert attrs["win_rate"] == 0.5  # Default neutral
        assert attrs["kill_death_ratio"] == 1.0  # Default neutral
        assert attrs["warnings_per_minute"] == 0.0
        assert attrs["game_mode"] == "FFA"
        assert attrs["controller_count"] == 4
        assert attrs["alive"] is True
        assert attrs["grace_period"] is False

    def test_context_with_profile_ffa_mode(self):
        """Test context building with profile for FFA mode."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            ffa_wins=7,
            ffa_total_games=10,  # 70% win rate
            nonstop_kills=20,
            nonstop_deaths=10,  # 2.0 K/D
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="FFA",
            controller_count=4,
            game_duration_seconds=60.0,
        )

        attrs = context.attributes
        assert attrs["win_rate"] == 0.7  # FFA win rate
        assert attrs["kill_death_ratio"] == 2.0  # Nonstop K/D

    def test_context_with_profile_joustffa_mode(self):
        """Test context building with JoustFFA mode uses FFA stats."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            ffa_wins=5,
            ffa_total_games=10,  # 50% win rate
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="JoustFFA",
            controller_count=4,
            game_duration_seconds=60.0,
        )

        attrs = context.attributes
        assert attrs["win_rate"] == 0.5

    def test_context_with_profile_team_mode(self):
        """Test context building with profile for team-based mode."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            team_wins=8,
            team_total_games=10,  # 80% win rate
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="Teams",
            controller_count=6,
            game_duration_seconds=120.0,
        )

        attrs = context.attributes
        assert attrs["win_rate"] == 0.8  # Team win rate

    def test_context_with_profile_nonstop_mode(self):
        """Test context building with Nonstop mode uses K/D for win rate."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            nonstop_kills=30,
            nonstop_deaths=10,  # 3.0 K/D
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="NonstopJoust",
            controller_count=4,
            game_duration_seconds=180.0,
        )

        attrs = context.attributes
        # K/D of 3.0 capped at 3.0 → win_rate = 3.0/3.0 = 1.0
        assert attrs["win_rate"] == 1.0
        assert attrs["kill_death_ratio"] == 3.0

    def test_context_with_profile_nonstop_kd_capped(self):
        """Test that Nonstop K/D is capped at 3.0 for win_rate."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            nonstop_kills=50,
            nonstop_deaths=10,  # 5.0 K/D
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="Nonstop",
            controller_count=4,
            game_duration_seconds=60.0,
        )

        attrs = context.attributes
        # K/D of 5.0 capped at 3.0 → win_rate = 3.0/3.0 = 1.0
        assert attrs["win_rate"] == 1.0
        assert attrs["kill_death_ratio"] == 5.0  # Not capped for K/D itself

    def test_warnings_per_minute_calculation(self):
        """Test warnings_per_minute is calculated correctly."""
        player = MockPlayer(warning_count=10)

        context = build_player_context(
            player=player,
            game_mode="FFA",
            controller_count=4,
            game_duration_seconds=120.0,  # 2 minutes
        )

        attrs = context.attributes
        # 10 warnings in 2 minutes = 5 warnings/minute
        assert attrs["warnings_per_minute"] == 5.0

    def test_warnings_per_minute_zero_duration(self):
        """Test warnings_per_minute is 0.0 when game_duration_seconds is 0."""
        player = MockPlayer(warning_count=10)

        context = build_player_context(
            player=player,
            game_mode="FFA",
            controller_count=4,
            game_duration_seconds=0.0,
        )

        attrs = context.attributes
        assert attrs["warnings_per_minute"] == 0.0

    def test_grace_period_active(self):
        """Test grace_period attribute when player has grace_until > 0."""
        player = MockPlayer(grace_until=5.0)

        context = build_player_context(
            player=player,
            game_mode="FFA",
            controller_count=4,
        )

        attrs = context.attributes
        assert attrs["grace_period"] is True

    def test_player_dead(self):
        """Test alive attribute when player is dead."""
        player = MockPlayer(alive=False)

        context = build_player_context(
            player=player,
            game_mode="FFA",
            controller_count=4,
        )

        attrs = context.attributes
        assert attrs["alive"] is False

    def test_werewolf_mode_uses_ffa_stats(self):
        """Test Werewolf mode uses FFA win rate."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            ffa_wins=6,
            ffa_total_games=10,
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="Werewolf",
            controller_count=8,
        )

        attrs = context.attributes
        assert attrs["win_rate"] == 0.6

    def test_traitor_mode_uses_ffa_stats(self):
        """Test Traitor mode uses FFA win rate."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            ffa_wins=4,
            ffa_total_games=10,
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="Traitor",
            controller_count=6,
        )

        attrs = context.attributes
        assert attrs["win_rate"] == 0.4

    def test_zombie_mode_uses_ffa_stats(self):
        """Test Zombie mode uses FFA win rate."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            ffa_wins=3,
            ffa_total_games=10,
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="Zombie",
            controller_count=5,
        )

        attrs = context.attributes
        assert attrs["win_rate"] == 0.3

    def test_unknown_game_mode_uses_team_stats(self):
        """Test unknown game mode defaults to team win rate."""
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=1000.0,
            last_seen=2000.0,
            team_wins=7,
            team_total_games=10,
        )

        player = MockPlayer(profile=profile)

        context = build_player_context(
            player=player,
            game_mode="UnknownMode",
            controller_count=4,
        )

        attrs = context.attributes
        assert attrs["win_rate"] == 0.7


class TestBuildGameContext:
    """Tests for build_game_context()"""

    def test_basic_game_context(self):
        """Test building game-level context."""
        context = build_game_context(
            game_mode="FFA",
            controller_count=4,
            game_duration_seconds=120.0,
        )

        assert context.targeting_key == "FFA"
        attrs = context.attributes
        assert attrs["game_mode"] == "FFA"
        assert attrs["controller_count"] == 4
        assert attrs["game_duration_seconds"] == 120.0

    def test_game_context_different_modes(self):
        """Test game context with different modes."""
        modes = ["Teams", "Nonstop", "Zombie", "Werewolf"]
        for mode in modes:
            context = build_game_context(
                game_mode=mode,
                controller_count=6,
                game_duration_seconds=180.0,
            )
            assert context.targeting_key == mode
            assert context.attributes["game_mode"] == mode
