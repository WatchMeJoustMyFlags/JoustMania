"""
Player Context Builder for Feature Flag Evaluation (Phase 52)

Creates OpenFeature EvaluationContext with player performance stats
for adaptive gameplay adjustments.
"""

import logging
from typing import TYPE_CHECKING

from openfeature.evaluation_context import EvaluationContext

if TYPE_CHECKING:
    from services.game_coordinator.games.base import Player

logger = logging.getLogger(__name__)


def build_player_context(
    player: "Player",
    game_mode: str,
    controller_count: int,
    game_duration_seconds: float = 0.0,
) -> EvaluationContext:
    """
    Build an EvaluationContext for a player for feature flag evaluation.

    Args:
        player: Player object with stats and analytics
        game_mode: Current game mode name (e.g., "FFA", "Teams")
        controller_count: Number of players in the game
        game_duration_seconds: Time elapsed since game start

    Returns:
        EvaluationContext with player attributes for flag targeting

    Context attributes:
        - serial: Controller serial number (targeting key)
        - win_rate: Player win rate (0-1, default 0.5 if no history)
        - kill_death_ratio: K/D ratio (default 1.0 if no history)
        - warnings_per_minute: Rate of warning events
        - game_mode: Current game mode
        - controller_count: Number of players
    """
    # Calculate warnings per minute
    warnings_per_minute = 0.0
    if game_duration_seconds > 0:
        warnings_per_minute = (player.warning_count * 60.0) / game_duration_seconds

    # Win rate and K/D from player profile (Issue #23)
    # If profile is available, use real stats; otherwise default to neutral
    win_rate = 0.5  # Default to 50% (neutral)
    kill_death_ratio = 1.0  # Default to 1.0 (neutral)

    if hasattr(player, "profile") and player.profile is not None:
        # Use real stats from Redis player profile
        # Choose win_rate based on game mode
        if game_mode in ["FFA", "JoustFFA", "Werewolf", "Traitor", "Zombie"]:
            win_rate = player.profile.ffa_win_rate
        elif game_mode in ["NonstopJoust", "Nonstop"]:
            # For Nonstop, use K/D as proxy for "win rate"
            # Map K/D (0-3) to win_rate (0-1)
            kd_capped = min(player.profile.nonstop_kd_ratio, 3.0)
            win_rate = kd_capped / 3.0
            kill_death_ratio = player.profile.nonstop_kd_ratio
        else:
            # Team-based modes
            win_rate = player.profile.team_win_rate

        # Use Nonstop K/D for kill_death_ratio
        kill_death_ratio = player.profile.nonstop_kd_ratio

        logger.debug(
            f"Using profile stats for {player.serial}: "
            f"win_rate={win_rate:.2f}, K/D={kill_death_ratio:.2f}"
        )

    # Build context
    context = EvaluationContext(
        targeting_key=player.serial,
        attributes={
            "serial": player.serial,
            "win_rate": win_rate,
            "kill_death_ratio": kill_death_ratio,
            "warnings_per_minute": warnings_per_minute,
            "game_mode": game_mode,
            "controller_count": controller_count,
            "alive": player.alive,
            "grace_period": player.grace_until > 0,  # Currently in grace period
        },
    )

    logger.debug(
        f"Player context: {player.serial} | win_rate={win_rate:.2f} | "
        f"K/D={kill_death_ratio:.2f} | warnings/min={warnings_per_minute:.1f}"
    )

    return context


def build_game_context(
    game_mode: str,
    controller_count: int,
    game_duration_seconds: float,
) -> EvaluationContext:
    """
    Build an EvaluationContext for game-level flags (not player-specific).

    Args:
        game_mode: Current game mode name
        controller_count: Number of players
        game_duration_seconds: Time elapsed since game start

    Returns:
        EvaluationContext for game-level flag evaluation
    """
    return EvaluationContext(
        targeting_key=game_mode,
        attributes={
            "game_mode": game_mode,
            "controller_count": controller_count,
            "game_duration_seconds": game_duration_seconds,
        },
    )
