"""
Tests for player context builder.
"""

from dataclasses import dataclass

from lib.player_context import build_player_context


# Mock Player class for testing
@dataclass
class MockPlayer:
    serial: str
    alive: bool = True
    grace_until: float = 0.0
    warning_count: int = 0
    analytics: None = None


def test_build_player_context_defaults():
    """Test building player context with default values."""
    player = MockPlayer(serial="AA:BB:CC:DD:EE:FF")

    context = build_player_context(
        player=player,
        game_duration_seconds=0.0,
    )

    assert context.targeting_key == "AA:BB:CC:DD:EE:FF"
    assert context.attributes["serial"] == "AA:BB:CC:DD:EE:FF"
    assert context.attributes["player_name"] == "AA:BB:CC:DD:EE:FF"
    assert context.attributes["win_rate"] == 0.5  # Default neutral
    assert context.attributes["kill_death_ratio"] == 1.0  # Default neutral
    assert context.attributes["warnings_per_minute"] == 0.0
    assert context.attributes["alive"] is True


def test_build_player_context_with_warnings():
    """Test that warnings_per_minute is calculated correctly."""
    player = MockPlayer(serial="AA:BB:CC:DD:EE:FF", warning_count=5)

    context = build_player_context(
        player=player,
        game_duration_seconds=60.0,  # 1 minute
    )

    # 5 warnings in 60 seconds = 5 warnings/minute
    assert context.attributes["warnings_per_minute"] == 5.0


def test_build_player_context_warnings_zero_duration():
    """Test that warnings_per_minute handles zero duration gracefully."""
    player = MockPlayer(serial="AA:BB:CC:DD:EE:FF", warning_count=3)

    context = build_player_context(
        player=player,
        game_duration_seconds=0.0,
    )

    # No division by zero - should be 0.0
    assert context.attributes["warnings_per_minute"] == 0.0


def test_build_player_context_grace_period():
    """Test that grace period is tracked in context."""
    player = MockPlayer(serial="AA:BB:CC:DD:EE:FF", grace_until=100.0)

    context = build_player_context(
        player=player,
        game_duration_seconds=0.0,
    )

    # grace_until > 0 means in grace period
    assert context.attributes["grace_period"] is True


def test_build_player_context_no_game_mode_or_controller_count():
    """Test that game_mode and controller_count are not in per-evaluation context."""
    player = MockPlayer(serial="AA:BB:CC:DD:EE:FF")

    context = build_player_context(
        player=player,
        game_duration_seconds=0.0,
    )

    # These come from transaction context now, not per-evaluation context
    assert "game_mode" not in context.attributes
    assert "controller_count" not in context.attributes
