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

    # Win rate and K/D - default to neutral values if no history
    # In Phase 49 (Redis Player Profiles), these will come from persistent storage
    win_rate = 0.5  # Default to 50% (neutral)
    kill_death_ratio = 1.0  # Default to 1.0 (neutral)

    # If player has analytics, we can use in-game stats
    if player.analytics:
        # Use playstyle as a proxy for performance in current game
        # This is a temporary heuristic until Phase 49 adds persistent profiles
        peak_accel = player.analytics.peak_accel
        if peak_accel > 3.0:
            # Very aggressive player - might be strong
            win_rate = 0.6
        elif peak_accel < 1.5:
            # Very passive player - might be struggling
            win_rate = 0.4

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
