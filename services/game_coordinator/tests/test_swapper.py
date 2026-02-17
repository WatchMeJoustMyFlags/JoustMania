"""
Unit tests for Swapper game mode.

Tests the core swapper mechanics:
- Team swapping on death
- Grace period after swap
- Win condition detection (all players on same team)
"""

import sys
import time
from pathlib import Path

import pytest

# Setup paths for imports
test_dir = Path(__file__).parent
service_dir = test_dir.parent
project_root = service_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(test_dir))

from conftest import EventCollector, MockControllerManagerService

from services.game_coordinator.games.swapper import SwapperGame


class TestSwapperTeamSwapping:
    """Test Swapper's team swapping mechanics."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game with 4 players (2 per team)."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},  # No auto-deaths, we'll trigger manually
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_001",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_initial_team_assignment(self, swapper_game):
        """Test that players are assigned to teams round-robin (0, 1, 0, 1)."""
        game, mock_controller_manager, _ = swapper_game

        # Manually initialize players (normally done by game loop)
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Verify round-robin assignment: 0, 1, 0, 1
        assert game.players["mock_controller_0"].team == 0
        assert game.players["mock_controller_1"].team == 1
        assert game.players["mock_controller_2"].team == 0
        assert game.players["mock_controller_3"].team == 1

    @pytest.mark.asyncio
    async def test_kill_player_impl_swaps_team(self, swapper_game):
        """Test that _kill_player_impl swaps player to the other team."""
        game, mock_controller_manager, event_collector = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Get initial team for player 0
        player = game.players["mock_controller_0"]
        initial_team = player.team
        assert initial_team == 0

        # Mock the gameplay stream (needed for color updates in _kill_player_impl)
        game.gameplay_stream = MockGameplayStream()

        # Assign team colors (needed for swap)
        await game._assign_team_colors()

        # Call _kill_player_impl directly
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)

        # Verify team swapped
        assert player.team == 1, f"Expected team 1, got {player.team}"
        assert player.team != initial_team

        # Verify swap event was published
        swap_events = event_collector.get_events_of_type("player_swapped")
        assert len(swap_events) == 1
        assert swap_events[0]["serial"] == "mock_controller_0"
        assert swap_events[0]["old_team"] == 0
        assert swap_events[0]["new_team"] == 1

    @pytest.mark.asyncio
    async def test_double_swap_returns_to_original_team(self, swapper_game):
        """Test that killing a player twice swaps them back to original team."""
        game, mock_controller_manager, event_collector = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        player = game.players["mock_controller_0"]
        initial_team = player.team
        assert initial_team == 0

        # Mock the gameplay stream
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # First swap: 0 -> 1
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert player.team == 1

        # Clear grace period for immediate second swap
        player.grace_until = 0

        # Second swap: 1 -> 0
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert player.team == 0, f"Expected team 0 after double swap, got {player.team}"

        # Verify two swap events
        swap_events = event_collector.get_events_of_type("player_swapped")
        assert len(swap_events) == 2

    @pytest.mark.asyncio
    async def test_grace_period_set_after_swap(self, swapper_game):
        """Test that player gets grace period after swapping."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        player = game.players["mock_controller_0"]

        # Mock the gameplay stream
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # Record time before swap
        time_before = time.time()

        # Swap player
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)

        # Verify grace period is ~2 seconds in the future
        assert player.grace_until > time_before + 1.5
        assert player.grace_until < time_before + 3.0

    @pytest.mark.asyncio
    async def test_win_condition_all_on_same_team(self, swapper_game):
        """Test that win condition triggers when all players on same team."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players (2 per team initially)
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initial state: team 0 has [0, 2], team 1 has [1, 3]
        assert not await game._check_win_condition()

        # Move all players to team 1
        for serial in game.players:
            game.players[serial].team = 1

        # Now should trigger win condition
        assert await game._check_win_condition()

    @pytest.mark.asyncio
    async def test_get_alive_teams(self, swapper_game):
        """Test _get_alive_teams returns teams with players."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initial: both teams have players
        teams = game._get_alive_teams()
        assert teams == {0, 1}

        # Move all to team 0
        for serial in game.players:
            game.players[serial].team = 0

        teams = game._get_alive_teams()
        assert teams == {0}

    @pytest.mark.asyncio
    async def test_last_death_serial_tracked(self, swapper_game):
        """Test that last_death_serial tracks the last player to swap."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Mock the gameplay stream
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # Initially no last death
        assert game.last_death_serial is None

        # Swap player 0
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert game.last_death_serial == "mock_controller_0"

        # Swap player 1 (clear grace first)
        game.players["mock_controller_1"].grace_until = 0
        await game._kill_player_impl("mock_controller_1", accel_mag=5.0)
        assert game.last_death_serial == "mock_controller_1"

    @pytest.mark.asyncio
    async def test_swap_count_tracked(self, swapper_game):
        """Test that swap_count is tracked per player."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        player = game.players["mock_controller_0"]

        # Mock the gameplay stream
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # Initially no swaps
        assert getattr(player, "swap_count", 0) == 0

        # First swap
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert player.swap_count == 1

        # Clear grace and swap again
        player.grace_until = 0
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)
        assert player.swap_count == 2

    @pytest.mark.asyncio
    async def test_swap_changes_color(self, swapper_game):
        """After swap, player color should match their new team's color."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Setup gameplay stream and assign team colors
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        # Record initial color for player 0
        player = game.players["mock_controller_0"]
        initial_color = player.color

        # Swap player 0 via kill
        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)

        # Color should have changed to new team's color
        assert player.color != initial_color
        new_team = player.team
        expected_color = game.team_colors[new_team]["rgb"]
        assert player.color == expected_color

    @pytest.mark.asyncio
    async def test_end_game_all_same_team(self, swapper_game):
        """When all players are on the same team, game ends."""
        game, mock_controller_manager, _ = swapper_game

        # Initialize players
        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Move all players to team 0
        for serial in game.players:
            game.players[serial].team = 0

        # Win condition should trigger
        assert await game._check_win_condition()


class MockGameplayStream:
    """Minimal mock for gameplay stream writes."""

    async def write(self, message):
        """Accept writes silently."""
        pass


class RecordingGameplayStream:
    """Mock gameplay stream that records all writes."""

    def __init__(self):
        self.messages = []

    async def write(self, message):
        self.messages.append(message)


class TestSwapperWinConditionExclusion:
    """Tests for last-swapper exclusion from winners."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_exclusion",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_last_swapper_excluded_from_winners(self, swapper_game):
        """Test that the player who caused the final swap is NOT a winner."""
        game, mock_controller_manager, event_collector = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Move all players to team 0
        for serial in game.players:
            game.players[serial].team = 0

        # Set last_death_serial (the final swapper)
        game.last_death_serial = "mock_controller_0"

        result = await game._check_win_condition()
        assert result is True

        # Verify the swapper_winner event excludes the last swapper
        winner_events = event_collector.get_events_of_type("swapper_winner")
        assert len(winner_events) == 1
        assert "mock_controller_0" not in winner_events[0]["winners"]
        assert winner_events[0]["excluded_serial"] == "mock_controller_0"
        # Other players should be winners
        assert "mock_controller_1" in winner_events[0]["winners"]
        assert "mock_controller_2" in winner_events[0]["winners"]
        assert "mock_controller_3" in winner_events[0]["winners"]

    @pytest.mark.asyncio
    async def test_win_condition_includes_team_info(self, swapper_game):
        """Test that win event includes correct team number and name."""
        game, mock_controller_manager, event_collector = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Move all players to team 1
        for serial in game.players:
            game.players[serial].team = 1

        game.last_death_serial = "mock_controller_2"

        await game._check_win_condition()

        winner_events = event_collector.get_events_of_type("swapper_winner")
        assert len(winner_events) == 1
        assert winner_events[0]["team"] == 1
        assert winner_events[0]["team_name"] == game.team_colors[1]["name"]


class TestSwapperGracePeriod:
    """Tests for grace period during team swap."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_grace",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_player_alive_after_swap(self, swapper_game):
        """Test that player is alive=True after completing a swap."""
        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        player = game.players["mock_controller_0"]
        assert player.alive is True

        await game._kill_player_impl("mock_controller_0", accel_mag=5.0)

        # Player should be alive again after swap completes
        assert player.alive is True

    @pytest.mark.asyncio
    async def test_grace_period_is_approximately_2_seconds(self, swapper_game):
        """Test that grace period is approximately 2 seconds from swap time."""
        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)
        game.gameplay_stream = MockGameplayStream()
        await game._assign_team_colors()

        player = game.players["mock_controller_1"]
        before = time.time()

        await game._kill_player_impl("mock_controller_1", accel_mag=5.0)

        # Grace period should be ~2 seconds from when swap was initiated
        assert player.grace_until >= before + 1.5
        assert player.grace_until <= before + 3.5  # Allow for asyncio.sleep(0.5) in swap


class TestSwapperEndGameImpl:
    """Tests for _end_game_impl winner effects and team sound."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game with 4 players."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_endgame",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_end_game_sends_rainbow_to_winners(self, swapper_game):
        """Test that _end_game_impl sends rainbow effect to winning players."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # All on team 0
        for serial in game.players:
            game.players[serial].team = 0

        game.last_death_serial = "mock_controller_3"  # Excluded

        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        # Rainbow effects should be sent to winners (not the excluded player)
        effect_msgs = [m for m in stream.messages if m.HasField("game_effect")]
        effect_serials = {m.game_effect.serial for m in effect_msgs}

        assert "mock_controller_0" in effect_serials
        assert "mock_controller_1" in effect_serials
        assert "mock_controller_2" in effect_serials
        assert "mock_controller_3" not in effect_serials

    @pytest.mark.asyncio
    async def test_end_game_dims_excluded_player(self, swapper_game):
        """Test that _end_game_impl sets dim gray on the excluded last-swapper."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.players:
            game.players[serial].team = 0

        game.last_death_serial = "mock_controller_2"

        stream = RecordingGameplayStream()
        game.gameplay_stream = stream
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        # Look for dim gray color command for excluded player
        color_msgs = [m for m in stream.messages if m.HasField("base_color")]
        excluded_colors = [m for m in color_msgs if m.base_color.serial == "mock_controller_2"]
        assert len(excluded_colors) >= 1
        assert excluded_colors[0].base_color.color.r == 50
        assert excluded_colors[0].base_color.color.g == 50
        assert excluded_colors[0].base_color.color.b == 50

    @pytest.mark.asyncio
    async def test_end_game_publishes_game_ended_event(self, swapper_game):
        """Test that _end_game_impl publishes game_ended with winning_team."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, event_collector = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.players:
            game.players[serial].team = 1

        game.last_death_serial = None
        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = time.time() - 30
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        ended_events = event_collector.get_events_of_type("game_ended")
        assert len(ended_events) == 1
        assert ended_events[0]["winning_team"] == 1
        assert ended_events[0]["game_id"] == "test_swapper_endgame"

    @pytest.mark.asyncio
    async def test_end_game_sets_state_ended(self, swapper_game):
        """Test that _end_game_impl transitions to ENDED state."""
        from services.game_coordinator.games.base import GameState

        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        for serial in game.players:
            game.players[serial].team = 0

        game.gameplay_stream = RecordingGameplayStream()
        game.start_time = time.time()
        game.state = GameState.RUNNING
        game.running = False

        await game._end_game_impl()

        assert game.state == GameState.ENDED


class TestSwapperSpanManagement:
    """Tests for hierarchical span recreation on team swap."""

    @pytest.fixture
    def swapper_game(self):
        """Create a Swapper game."""
        mock_controller_manager = MockControllerManagerService(
            num_controllers=4,
            death_schedule={},
            max_duration=10.0,
        )
        event_collector = EventCollector()

        game = SwapperGame(
            controller_manager_client=mock_controller_manager,
            event_publisher=event_collector.publish,
            audio_client=None,
            game_id="test_swapper_spans",
        )

        return game, mock_controller_manager, event_collector

    @pytest.mark.asyncio
    async def test_get_team_counts(self, swapper_game):
        """Test _get_team_counts returns correct per-team player counts."""
        game, mock_controller_manager, _ = swapper_game

        await game._initialize_players_impl(mock_controller_manager.controllers)

        # Initial round-robin: 2 on team 0, 2 on team 1
        counts = game._get_team_counts()
        assert counts[0] == 2
        assert counts[1] == 2

        # Move one player to team 1
        game.players["mock_controller_0"].team = 1
        counts = game._get_team_counts()
        assert counts[0] == 1
        assert counts[1] == 3

    @pytest.mark.asyncio
    async def test_get_game_name(self, swapper_game):
        """Test get_game_name returns 'Swapper'."""
        game, _, _ = swapper_game
        assert game.get_game_name() == "Swapper"
