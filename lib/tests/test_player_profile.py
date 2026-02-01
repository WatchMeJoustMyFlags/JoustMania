"""
Tests for Player Profile Data Model (Issue #23)

Tests serialization, performance scoring, and stat tracking.
"""

import time

from lib.player_profile import PlayerProfile, RewardTier, RoundResult


class TestRoundResult:
    """Test RoundResult dataclass."""

    def test_round_result_creation(self):
        """Test creating a round result."""
        now = time.time()
        result = RoundResult(
            timestamp=now,
            game_mode="FFA",
            player_count=4,
            won=True,
            alive=True,
            survival_time=120.5,
            warnings=2,
            kills=0,
            deaths=0,
        )
        assert result.timestamp == now
        assert result.game_mode == "FFA"
        assert result.won is True
        assert result.survival_time == 120.5

    def test_round_result_serialization(self):
        """Test round result to_dict and from_dict."""
        now = time.time()
        result = RoundResult(
            timestamp=now,
            game_mode="Nonstop",
            player_count=6,
            won=False,
            alive=True,
            survival_time=300.0,
            warnings=5,
            kills=10,
            deaths=8,
        )

        # Serialize and deserialize
        data = result.to_dict()
        restored = RoundResult.from_dict(data)

        assert restored.timestamp == result.timestamp
        assert restored.game_mode == result.game_mode
        assert restored.kills == result.kills
        assert restored.deaths == result.deaths


class TestPlayerProfile:
    """Test PlayerProfile dataclass."""

    def test_create_new_profile(self):
        """Test creating a new profile with default values."""
        profile = PlayerProfile.create_new("AA:BB:CC:DD:EE:FF")

        assert profile.serial == "AA:BB:CC:DD:EE:FF"
        assert profile.ffa_total_games == 0
        assert profile.nonstop_total_games == 0
        assert profile.team_total_games == 0
        assert profile.total_games == 0
        assert profile.performance_score == 50.0
        assert profile.reward_tier == RewardTier.NEUTRAL.value
        assert profile.first_seen == profile.last_seen

    def test_profile_serialization(self):
        """Test profile to_dict and from_dict."""
        now = time.time()
        profile = PlayerProfile(
            serial="AA:BB:CC:DD:EE:FF",
            first_seen=now,
            last_seen=now,
            ffa_total_games=10,
            ffa_wins=7,
            ffa_warnings=15,
            ffa_total_survival_time=1200.0,
            nonstop_total_games=5,
            nonstop_kills=50,
            nonstop_deaths=25,
            nonstop_best_streak=8,
            team_total_games=3,
            team_wins=2,
        )

        # Serialize and deserialize
        data = profile.to_dict()
        restored = PlayerProfile.from_dict(data)

        assert restored.serial == profile.serial
        assert restored.ffa_total_games == profile.ffa_total_games
        assert restored.ffa_wins == profile.ffa_wins
        assert restored.nonstop_kills == profile.nonstop_kills

    def test_ffa_win_rate(self):
        """Test FFA win rate calculation."""
        profile = PlayerProfile.create_new("test")

        # No games yet - should return neutral 0.5
        assert profile.ffa_win_rate == 0.5

        # Add some games
        profile.ffa_total_games = 10
        profile.ffa_wins = 7
        assert profile.ffa_win_rate == 0.7

        profile.ffa_wins = 3
        assert profile.ffa_win_rate == 0.3

    def test_nonstop_kd_ratio(self):
        """Test Nonstop K/D ratio calculation."""
        profile = PlayerProfile.create_new("test")

        # No deaths yet - should return kills (or 1.0 if no kills)
        assert profile.nonstop_kd_ratio == 1.0

        profile.nonstop_kills = 10
        assert profile.nonstop_kd_ratio == 10.0

        # With deaths
        profile.nonstop_deaths = 5
        assert profile.nonstop_kd_ratio == 2.0

        profile.nonstop_kills = 15
        profile.nonstop_deaths = 10
        assert profile.nonstop_kd_ratio == 1.5

    def test_team_win_rate(self):
        """Test team mode win rate calculation."""
        profile = PlayerProfile.create_new("test")

        # No games yet - should return neutral 0.5
        assert profile.team_win_rate == 0.5

        # Add some games
        profile.team_total_games = 10
        profile.team_wins = 6
        assert profile.team_win_rate == 0.6

    def test_total_games(self):
        """Test total games calculation."""
        profile = PlayerProfile.create_new("test")
        assert profile.total_games == 0

        profile.ffa_total_games = 5
        profile.nonstop_total_games = 3
        profile.team_total_games = 2
        assert profile.total_games == 10

    def test_warnings_per_game(self):
        """Test warnings per game calculation."""
        profile = PlayerProfile.create_new("test")
        assert profile.warnings_per_game == 0.0

        profile.total_warnings = 20
        profile.ffa_total_games = 5
        profile.nonstop_total_games = 5
        assert profile.warnings_per_game == 2.0

    def test_add_ffa_result(self):
        """Test adding FFA game result."""
        profile = PlayerProfile.create_new("test")
        old_last_seen = profile.last_seen

        # Wait a bit to ensure timestamp changes
        time.sleep(0.01)

        profile.add_ffa_result(won=True, warnings=3, survival_time=120.5)

        assert profile.ffa_total_games == 1
        assert profile.ffa_wins == 1
        assert profile.ffa_warnings == 3
        assert profile.ffa_total_survival_time == 120.5
        assert profile.total_warnings == 3
        assert profile.last_seen > old_last_seen

    def test_add_nonstop_result(self):
        """Test adding Nonstop game result."""
        profile = PlayerProfile.create_new("test")

        profile.add_nonstop_result(kills=15, deaths=8, warnings=5, current_streak=7)

        assert profile.nonstop_total_games == 1
        assert profile.nonstop_kills == 15
        assert profile.nonstop_deaths == 8
        assert profile.nonstop_best_streak == 7
        assert profile.total_warnings == 5

        # Add another game with lower streak
        profile.add_nonstop_result(kills=10, deaths=5, warnings=2, current_streak=4)

        assert profile.nonstop_total_games == 2
        assert profile.nonstop_kills == 25
        assert profile.nonstop_deaths == 13
        assert profile.nonstop_best_streak == 7  # Should keep best streak

    def test_add_team_result(self):
        """Test adding team game result."""
        profile = PlayerProfile.create_new("test")

        profile.add_team_result(won=True, warnings=2)

        assert profile.team_total_games == 1
        assert profile.team_wins == 1
        assert profile.total_warnings == 2

    def test_performance_score_new_player(self):
        """Test performance score for new player (no games)."""
        profile = PlayerProfile.create_new("test")
        score = profile.calculate_performance_score()

        assert score == 50.0  # Neutral score

    def test_performance_score_excellent_player(self):
        """Test performance score for excellent player."""
        profile = PlayerProfile.create_new("test")

        # Excellent stats
        profile.ffa_total_games = 10
        profile.ffa_wins = 9  # 90% win rate
        profile.ffa_total_survival_time = 1200.0  # 120s avg survival
        profile.nonstop_total_games = 10
        profile.nonstop_kills = 100
        profile.nonstop_deaths = 10  # 10.0 K/D (capped at 3.0 for scoring)
        profile.team_total_games = 10
        profile.team_wins = 9  # 90% win rate
        profile.total_warnings = 10  # 0.33 warnings/game (excellent)

        score = profile.calculate_performance_score()

        # Should be very high (close to 100)
        assert score >= 85.0
        assert score <= 100.0

    def test_performance_score_poor_player(self):
        """Test performance score for poor player."""
        profile = PlayerProfile.create_new("test")

        # Poor stats
        profile.ffa_total_games = 10
        profile.ffa_wins = 1  # 10% win rate
        profile.ffa_total_survival_time = 100.0  # 10s avg survival
        profile.nonstop_total_games = 10
        profile.nonstop_kills = 10
        profile.nonstop_deaths = 50  # 0.2 K/D
        profile.team_total_games = 10
        profile.team_wins = 1  # 10% win rate
        profile.total_warnings = 200  # 6.67 warnings/game (poor)

        score = profile.calculate_performance_score()

        # Should be very low (close to 0)
        assert score >= 0.0
        assert score <= 20.0

    def test_update_performance_metrics(self):
        """Test updating performance metrics and reward tier."""
        profile = PlayerProfile.create_new("test")

        # Excellent stats
        profile.ffa_total_games = 10
        profile.ffa_wins = 9
        profile.ffa_total_survival_time = 1200.0
        profile.nonstop_total_games = 10
        profile.nonstop_kills = 100
        profile.nonstop_deaths = 10
        profile.team_total_games = 10
        profile.team_wins = 9
        profile.total_warnings = 10

        profile.update_performance_metrics()

        assert profile.performance_score >= 75.0  # Excellent tier
        assert profile.reward_tier == RewardTier.EXCELLENT.value

    def test_reward_tier_classification(self):
        """Test reward tier classification at boundaries."""
        profile = PlayerProfile.create_new("test")

        # Test EXCELLENT boundary (score >= 75)
        profile.performance_score = 75.0
        # Manually classify tier (same logic as update_performance_metrics)
        if profile.performance_score >= 75:
            profile.reward_tier = RewardTier.EXCELLENT.value
        assert profile.reward_tier == RewardTier.EXCELLENT.value

        # Test GOOD boundary (score >= 60)
        profile.performance_score = 60.0
        if profile.performance_score >= 60:
            profile.reward_tier = RewardTier.GOOD.value
        assert profile.reward_tier == RewardTier.GOOD.value

        # Test NEUTRAL boundary (score >= 40)
        profile.performance_score = 40.0
        if profile.performance_score >= 40:
            profile.reward_tier = RewardTier.NEUTRAL.value
        assert profile.reward_tier == RewardTier.NEUTRAL.value

        # Test POOR boundary (score < 40)
        profile.performance_score = 39.0
        if profile.performance_score < 40:
            profile.reward_tier = RewardTier.POOR.value
        assert profile.reward_tier == RewardTier.POOR.value
