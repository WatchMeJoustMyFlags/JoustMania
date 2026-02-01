"""
Player Profile Data Model for JoustMania (Issue #23)

Tracks player performance across game sessions for adaptive rewards
and performance scoring.
"""

import time
from dataclasses import asdict, dataclass
from enum import Enum


class RewardTier(Enum):
    """Performance-based reward tiers for adaptive gameplay."""

    EXCELLENT = "EXCELLENT"  # Top 10% of players
    GOOD = "GOOD"  # Top 30% of players
    NEUTRAL = "NEUTRAL"  # Middle 40% of players
    POOR = "POOR"  # Bottom 30% of players


@dataclass
class RoundResult:
    """Result of a single game round for history tracking."""

    timestamp: float  # Unix timestamp
    game_mode: str  # "FFA", "Teams", "Nonstop", etc.
    player_count: int  # Number of players in game
    won: bool  # Did player win?
    alive: bool  # Was player alive at end?
    survival_time: float  # Seconds survived
    warnings: int  # Number of warnings received
    kills: int = 0  # Kills (for Nonstop mode)
    deaths: int = 0  # Deaths (for Nonstop mode)

    def to_dict(self) -> dict:
        """Serialize to dictionary for Redis storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RoundResult":
        """Deserialize from dictionary."""
        return cls(**data)


@dataclass
class PlayerProfile:
    """
    Persistent player profile with stats across all game modes.

    Tracks cumulative performance for adaptive rewards, experimentation,
    and player insights dashboards.
    """

    serial: str  # Controller serial number (MAC address)
    first_seen: float  # Unix timestamp of first game
    last_seen: float  # Unix timestamp of last game

    # FFA stats
    ffa_total_games: int = 0
    ffa_wins: int = 0
    ffa_warnings: int = 0
    ffa_total_survival_time: float = 0.0  # Total seconds survived across all games

    # Nonstop Joust stats
    nonstop_total_games: int = 0
    nonstop_kills: int = 0
    nonstop_deaths: int = 0
    nonstop_best_streak: int = 0

    # Team stats (all team-based modes)
    team_total_games: int = 0
    team_wins: int = 0

    # Hardware/connection stats
    total_warnings: int = 0  # Across all modes
    average_battery_level: float = 100.0  # Percentage
    connection_stability_score: float = 100.0  # 0-100

    # Performance metrics
    performance_score: float = 50.0  # 0-100, calculated
    reward_tier: str = RewardTier.NEUTRAL.value

    def to_dict(self) -> dict:
        """Serialize to dictionary for Redis storage."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PlayerProfile":
        """Deserialize from dictionary."""
        return cls(**data)

    @classmethod
    def create_new(cls, serial: str) -> "PlayerProfile":
        """
        Create a new player profile with default values.

        Args:
            serial: Controller serial number

        Returns:
            New PlayerProfile instance
        """
        now = time.time()
        return cls(
            serial=serial,
            first_seen=now,
            last_seen=now,
        )

    @property
    def ffa_win_rate(self) -> float:
        """Calculate FFA win rate (0.0 to 1.0)."""
        if self.ffa_total_games == 0:
            return 0.5  # Default to neutral
        return self.ffa_wins / self.ffa_total_games

    @property
    def ffa_avg_survival_time(self) -> float:
        """Calculate average FFA survival time in seconds."""
        if self.ffa_total_games == 0:
            return 0.0
        return self.ffa_total_survival_time / self.ffa_total_games

    @property
    def nonstop_kd_ratio(self) -> float:
        """Calculate Nonstop K/D ratio."""
        if self.nonstop_deaths == 0:
            return float(self.nonstop_kills) if self.nonstop_kills > 0 else 1.0
        return self.nonstop_kills / self.nonstop_deaths

    @property
    def team_win_rate(self) -> float:
        """Calculate team mode win rate (0.0 to 1.0)."""
        if self.team_total_games == 0:
            return 0.5  # Default to neutral
        return self.team_wins / self.team_total_games

    @property
    def total_games(self) -> int:
        """Total games played across all modes."""
        return self.ffa_total_games + self.nonstop_total_games + self.team_total_games

    @property
    def warnings_per_game(self) -> float:
        """Average warnings per game."""
        if self.total_games == 0:
            return 0.0
        return self.total_warnings / self.total_games

    def update_last_seen(self) -> None:
        """Update last_seen timestamp to now."""
        self.last_seen = time.time()

    def add_ffa_result(
        self,
        won: bool,
        warnings: int,
        survival_time: float,
    ) -> None:
        """
        Add FFA game result to profile.

        Args:
            won: Did player win?
            warnings: Number of warnings received
            survival_time: Seconds survived
        """
        self.ffa_total_games += 1
        if won:
            self.ffa_wins += 1
        self.ffa_warnings += warnings
        self.ffa_total_survival_time += survival_time
        self.total_warnings += warnings
        self.update_last_seen()

    def add_nonstop_result(
        self,
        kills: int,
        deaths: int,
        warnings: int,
        current_streak: int,
    ) -> None:
        """
        Add Nonstop Joust game result to profile.

        Args:
            kills: Number of kills
            deaths: Number of deaths
            warnings: Number of warnings
            current_streak: Best kill streak this game
        """
        self.nonstop_total_games += 1
        self.nonstop_kills += kills
        self.nonstop_deaths += deaths
        self.nonstop_best_streak = max(self.nonstop_best_streak, current_streak)
        self.total_warnings += warnings
        self.update_last_seen()

    def add_team_result(
        self,
        won: bool,
        warnings: int,
    ) -> None:
        """
        Add team mode game result to profile.

        Args:
            won: Did player's team win?
            warnings: Number of warnings received
        """
        self.team_total_games += 1
        if won:
            self.team_wins += 1
        self.total_warnings += warnings
        self.update_last_seen()

    def calculate_performance_score(self) -> float:
        """
        Calculate performance score (0-100) based on all stats.

        Weights:
        - Win rate (40%): Average of FFA and Team win rates
        - K/D ratio (20%): Nonstop K/D (capped at 3.0 for scoring)
        - Survival (20%): FFA average survival time (capped at 120s)
        - Warnings (20%): Inverse of warnings per game (fewer = better)

        Returns:
            Performance score from 0-100
        """
        if self.total_games == 0:
            return 50.0  # Default neutral score

        # Win rate component (0-40 points)
        avg_win_rate = (self.ffa_win_rate + self.team_win_rate) / 2
        win_score = avg_win_rate * 40

        # K/D component (0-20 points) - cap at 3.0 for scoring
        kd_capped = min(self.nonstop_kd_ratio, 3.0)
        kd_score = (kd_capped / 3.0) * 20

        # Survival component (0-20 points) - cap at 120 seconds
        survival_capped = min(self.ffa_avg_survival_time, 120.0)
        survival_score = (survival_capped / 120.0) * 20

        # Warnings component (0-20 points) - inverse, fewer is better
        # Assume 5 warnings/game is "normal", scale down from there
        if self.warnings_per_game <= 2.0:
            warning_score = 20.0  # Excellent
        elif self.warnings_per_game <= 5.0:
            # Linear scale from 20 to 10
            warning_score = 20.0 - ((self.warnings_per_game - 2.0) / 3.0) * 10.0
        elif self.warnings_per_game <= 10.0:
            # Linear scale from 10 to 0
            warning_score = 10.0 - ((self.warnings_per_game - 5.0) / 5.0) * 10.0
        else:
            warning_score = 0.0  # Very poor

        total_score = win_score + kd_score + survival_score + warning_score
        return round(total_score, 2)

    def update_performance_metrics(self) -> None:
        """
        Update performance_score and reward_tier based on current stats.

        Call this after adding game results.
        """
        self.performance_score = self.calculate_performance_score()

        # Classify into reward tier
        if self.performance_score >= 75:
            self.reward_tier = RewardTier.EXCELLENT.value
        elif self.performance_score >= 60:
            self.reward_tier = RewardTier.GOOD.value
        elif self.performance_score >= 40:
            self.reward_tier = RewardTier.NEUTRAL.value
        else:
            self.reward_tier = RewardTier.POOR.value
