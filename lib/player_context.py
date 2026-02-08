"""
Player Context Builder for Feature Flag Evaluation

Creates OpenFeature EvaluationContext with player performance stats
for adaptive gameplay adjustments. Used as per-evaluation context
that merges with API-level and transaction contexts.
"""

import logging
from typing import TYPE_CHECKING

from openfeature.evaluation_context import EvaluationContext

if TYPE_CHECKING:
    from services.game_coordinator.games.base import Player

logger = logging.getLogger(__name__)


def build_player_context(
    player: "Player",
    game_duration_seconds: float = 0.0,
) -> EvaluationContext:
    """
    Build an EvaluationContext for a player for feature flag evaluation.

    This is the per-evaluation context layer. Game-level attributes
    (game_mode, controller_count) come from the transaction context
    set by set_game_transaction_context().

    Args:
        player: Player object with stats and analytics
        game_duration_seconds: Time elapsed since game start

    Returns:
        EvaluationContext with player attributes for flag targeting

    Context attributes:
        - serial: Controller serial number (targeting key)
        - win_rate: Player win rate (0-1, default 0.5 if no history)
        - kill_death_ratio: K/D ratio (default 1.0 if no history)
        - warnings_per_minute: Rate of warning events
        - player_name: Player display name (serial as fallback)
        - alive: Whether player is currently alive
        - grace_period: Whether player is in grace period
    """
    # Calculate warnings per minute
    warnings_per_minute = 0.0
    if game_duration_seconds > 0:
        warnings_per_minute = (player.warning_count * 60.0) / game_duration_seconds

    # Win rate and K/D - default to neutral values if no history
    win_rate = 0.5  # Default to 50% (neutral)
    kill_death_ratio = 1.0  # Default to 1.0 (neutral)

    # If player has analytics, use in-game stats as heuristic
    if player.analytics:
        peak_accel = player.analytics.peak_accel
        if peak_accel > 3.0:
            # Very aggressive player - might be strong
            win_rate = 0.6
        elif peak_accel < 1.5:
            # Very passive player - might be struggling
            win_rate = 0.4

    # Use serial as player name fallback
    player_name = player.serial

    # Build context
    context = EvaluationContext(
        targeting_key=player.serial,
        attributes={
            "serial": player.serial,
            "player_name": player_name,
            "win_rate": win_rate,
            "kill_death_ratio": kill_death_ratio,
            "warnings_per_minute": warnings_per_minute,
            "alive": player.alive,
            "grace_period": player.grace_until > 0,
        },
    )

    logger.debug(
        f"Player context: {player.serial} | win_rate={win_rate:.2f} | "
        f"K/D={kill_death_ratio:.2f} | warnings/min={warnings_per_minute:.1f}"
    )

    return context
