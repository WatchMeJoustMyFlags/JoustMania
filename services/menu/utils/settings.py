"""Settings utilities for the Menu service."""

import logging

from lib.types import Games

logger = logging.getLogger(__name__)


# Game modes available in the menu (from Games enum, excluding Random which is meta)
GAME_MODES: list[str] = [g.name for g in Games if g != Games.Random]

DEFAULT_GAME_MODE: Games = Games.JoustFFA
DEFAULT_VOICE_ACTOR: str = "ivy"


def get_next_game_mode(current: Games, forward: bool = True) -> Games:
    """
    Get the next game mode in the cycle.

    Args:
        current: Games enum value
        forward: True to cycle forward, False to cycle backward

    Returns:
        Next Games enum value
    """
    current_name = current.name
    current_index = GAME_MODES.index(current_name) if current_name in GAME_MODES else 0

    if forward:
        next_name = GAME_MODES[(current_index + 1) % len(GAME_MODES)]
    else:
        next_name = GAME_MODES[(current_index - 1) % len(GAME_MODES)]

    # Convert back to Games enum
    return Games.from_name(next_name) or DEFAULT_GAME_MODE


def is_valid_game_mode(game_mode: Games) -> bool:
    """
    Check if a game mode is valid.

    Args:
        game_mode: Games enum value

    Returns:
        True if valid
    """
    return game_mode.name in GAME_MODES
