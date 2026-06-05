"""
Comprehensive integration tests for all JoustMania game modes.

Tests full game lifecycle: Menu -> Game Start -> Gameplay -> Game End -> Back to Menu
with LED color verification at each stage.

Requires PR #165 observability API: GetColor, StreamObservability
"""

import asyncio
import os
import sys
from collections.abc import Callable
from typing import Any

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from tests.integration.helpers import (
    GameEventCollector,
    ObservabilityObserver,
    force_end_game,
    get_game_client,
    get_mock_client,
    get_mock_controller_serials,
    kill_players_for_team_win,
    kill_players_until_one_remains,
    setup_mock_controllers,
    start_game_via_menu,
    verify_controllers_have_color,
    verify_lobby_colors_restored,
    wait_for_lobby_colors,
)

# =============================================================================
# Test timing configuration
# =============================================================================
# Game settings are now stored in Menu service's state_manager and passed via
# typed proto config when starting games. Integration tests use default values.

# =============================================================================
# End strategy type and helpers
# =============================================================================

# Type alias for end strategy functions
# Signature: async def(mock_client, serials, game_client, event_collector) -> None
EndStrategy = Callable[[Any, list[str], Any, GameEventCollector], Any]


async def configure_test_settings(docker_compose, game_mode: str):
    """Configure game-specific settings for faster test execution.

    Note: Settings are now stored in Menu service's state_manager and passed
    via typed proto config. Integration tests use default settings.
    This function is kept for API compatibility but is now a no-op.

    Args:
        docker_compose: Docker compose fixture (unused)
        game_mode: The game mode being tested (unused)
    """
    # Settings are now passed via StartGameConfig proto from Menu service
    # Default settings are used for integration tests
    pass


async def end_ffa_game(mock_client, serials: list[str], game_client, _event_collector) -> None:
    """End FFA game by killing all but one player (verified kills)."""
    await kill_players_until_one_remains(mock_client, serials, delay=0.1, game_client=game_client)


async def end_team_game(mock_client, serials: list[str], game_client, _event_collector) -> None:
    """End team game by eliminating one team (verified kills)."""
    await kill_players_for_team_win(mock_client, serials, delay=0.1, game_client=game_client)


async def end_with_force(_mock_client, _serials: list[str], game_client, event_collector) -> None:
    """End game via ForceEndGame RPC."""
    await asyncio.sleep(2.0)  # Let game run briefly
    await force_end_game(game_client, event_collector, timeout=10)


# =============================================================================
# Game mode configurations - single list with callable end strategies
# =============================================================================

# Thin sequential menu-flow set (#826): only 2 representative modes run through
# the full menu flow here, because menu ready-up, mode selection, and
# lobby-color *restore* are single-lobby menu behavior that cannot parallelize.
# JoustFFA (per-elimination FFA) and JoustTeams (team win) are kept as the
# representatives. Per-mode game-logic coverage for ALL OTHER modes
# (JoustRandomTeams, Swapper, Zombies, Werewolf, Tournament, FightClub, NonStop,
# Traitor) moved to headless parallel batches in test_parallel_lifecycle.py.
#
# Note: FightClub's menu-start flake (#757) is deliberately dodged by moving it
# to the headless set — it no longer runs through the flaky menu start here.
#
# Format: (game_mode, min_players, end_strategy_fn, timeout_seconds)
ALL_GAME_MODES = [
    # FFA games - kill until one remains
    pytest.param("JoustFFA", 2, end_ffa_game, 15, id="JoustFFA"),
    # Team games - eliminate one team
    pytest.param("JoustTeams", 3, end_team_game, 15, id="JoustTeams"),
]


# =============================================================================
# Main game lifecycle test
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("game_mode,_min_players,end_strategy,game_timeout", ALL_GAME_MODES)
async def test_full_game_lifecycle(
    docker_compose, game_mode: str, _min_players: int, end_strategy: EndStrategy, game_timeout: int
):
    """Test the full menu-driven lifecycle for the representative modes.

    This is the thin SEQUENTIAL menu-flow set (#826): only JoustFFA and
    JoustTeams run here, exercising the single-lobby menu behavior that cannot
    parallelize (ready-up, mode selection, lobby-color restore). Per-mode game
    logic for every other mode runs headless and in parallel in
    test_parallel_lifecycle.py.

    Each mode has its own end strategy:
    - FFA games: Kill until one player remains
    - Team games: Eliminate one team

    Verifies:
    1. Game starts via Menu flow
    2. Players get game colors (non-zero)
    3. End strategy triggers win condition
    4. Game ends (naturally or via force)
    5. Menu resets LED colors (not stuck at black)

    Args:
        game_mode: Name of the game mode to test
        _min_players: Minimum players required (for documentation, unused in test)
        end_strategy: Async function to trigger game end
        game_timeout: Timeout for game end in seconds
    """
    # Configure game-specific settings for faster test execution
    await configure_test_settings(docker_compose, game_mode)

    # Ensure mock controllers exist (RPC-based, no longer pre-created at startup)
    await setup_mock_controllers(docker_compose, count=4)

    # Get clients
    mock_client, mock_channel = await get_mock_client(docker_compose)
    game_client, game_channel = await get_game_client(docker_compose)
    serials = await get_mock_controller_serials(docker_compose)

    # Use context managers for clean resource management
    async with GameEventCollector(game_client) as event_collector:
        observer = ObservabilityObserver(mock_client)
        await observer.start()

        try:
            # 1. Start game via Menu flow (uses event_collector for reliable event detection)
            await start_game_via_menu(
                docker_compose,
                game_mode=game_mode,
                timeout=25.0,
                event_collector=event_collector,
            )

            # 2. Brief pause for game colors to be applied
            await asyncio.sleep(0.5)

            # 3. Verify all controllers have some color (game assigned)
            await verify_controllers_have_color(mock_client, serials)

            # 4. Apply end strategy
            print(f"Applying end strategy for {game_mode}")
            await end_strategy(mock_client, serials, game_client, event_collector)

            # 5. Wait for game to end (if not already ended by force)
            if end_strategy != end_with_force:
                try:
                    await event_collector.wait_for_any_event(
                        ["game_ended", "game_force_ended", "game_error"],
                        timeout=game_timeout
                    )
                except TimeoutError:
                    # Debug: print collected events before re-raising
                    print(f"DEBUG: Collected {len(event_collector.events)} events:")
                    for event in event_collector.events:
                        print(f"  - {event.event_type}: {dict(event.data)}")
                    raise

            # 6+7. Wait for menu to reset controller colors (poll instead of a
            # fixed sleep — menu reset timing varies under CI load, #757)
            await wait_for_lobby_colors(mock_client, serials, timeout=10.0)

            # 8. Verify event sequence shows lobby colors restored
            events = observer.get_events()
            verify_lobby_colors_restored(events, serials)

        finally:
            await observer.stop()

    await game_channel.close()
    await mock_channel.close()
